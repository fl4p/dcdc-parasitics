#!/usr/bin/env python3
"""Deterministic closed-volume Gmsh fixtures for Palace electrostatics."""
from dataclasses import asdict, dataclass
import json
from pathlib import Path

import numpy as np
from shapely.geometry import Polygon

if __package__:
    from .provenance import canonical_equal, canonical_sha256, file_sha256
else:
    from provenance import canonical_equal, canonical_sha256, file_sha256


MESH_MANIFEST_FORMAT = "dcdc-palace-mesh-v2"


def _active_tetrahedral_mesh_counts(gmsh):
    node_count = len(gmsh.model.mesh.getNodes()[0])
    element_types, element_tags, _ = gmsh.model.mesh.getElements(3)
    if any(element_type != 4 for element_type in element_types):
        raise ValueError("Palace mesh contains non-tetrahedral volume elements")
    tetrahedron_count = sum(len(tags) for tags in element_tags)
    gmsh.model.mesh.createEdges()
    edge_count = len(gmsh.model.mesh.getAllEdges()[0])
    gmsh.model.mesh.createFaces()
    face_count = len(gmsh.model.mesh.getAllFaces(3)[0])
    if min(node_count, edge_count, face_count, tetrahedron_count) <= 0:
        raise ValueError("Palace tetrahedral topology counts must be positive")
    return node_count, edge_count, face_count, tetrahedron_count


def tetrahedral_mesh_counts(mesh_path):
    import gmsh

    mesh_path = Path(mesh_path).resolve()
    initialized_here = not gmsh.isInitialized()
    if initialized_here:
        gmsh.initialize()
    try:
        gmsh.clear()
        gmsh.open(str(mesh_path))
        return _active_tetrahedral_mesh_counts(gmsh)
    except Exception as error:
        if isinstance(error, ValueError):
            raise
        raise ValueError(f"invalid Palace tetrahedral mesh: {error}") from error
    finally:
        gmsh.clear()
        if initialized_here:
            gmsh.finalize()


def _finite_scalar(value, *, label):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not np.isfinite(value)):
        raise ValueError(f"{label} must be a finite numeric scalar")
    return float(value)


@dataclass(frozen=True)
class BoxBounds:
    minimum: tuple[float, float, float]
    maximum: tuple[float, float, float]

    def __post_init__(self):
        minimum = tuple(
            _finite_scalar(value, label="box coordinate") for value in self.minimum
        )
        maximum = tuple(
            _finite_scalar(value, label="box coordinate") for value in self.maximum
        )
        if (len(minimum) != 3 or len(maximum) != 3
                or any(high <= low for low, high in zip(minimum, maximum))):
            raise ValueError("box bounds must be finite increasing 3D points")
        object.__setattr__(self, "minimum", minimum)
        object.__setattr__(self, "maximum", maximum)

    @property
    def lengths(self):
        return tuple(high - low for low, high in zip(self.minimum, self.maximum))


@dataclass(frozen=True)
class ConductorPrism:
    name: str
    rings: tuple[tuple[tuple[float, float], ...], ...]
    z_min: float
    z_max: float

    def __post_init__(self):
        rings = tuple(
            tuple(tuple(_finite_scalar(value, label="conductor coordinate")
                        for value in point) for point in ring)
            for ring in self.rings
        )
        if (not isinstance(self.name, str) or not self.name or not rings
                or any(len(ring) < 3 for ring in rings)):
            raise ValueError("conductor name and polygon rings are required")
        if any(len(point) != 2 for ring in rings for point in ring):
            raise ValueError("conductor polygon points must be 2D")
        if not np.isfinite(np.asarray([point for ring in rings for point in ring])).all():
            raise ValueError("conductor polygon coordinates must be finite")
        polygon = Polygon(rings[0], rings[1:])
        if not polygon.is_valid or polygon.is_empty or polygon.area <= 0.0:
            raise ValueError("conductor polygon must be valid and have positive area")
        z_min = _finite_scalar(self.z_min, label="conductor z_min")
        z_max = _finite_scalar(self.z_max, label="conductor z_max")
        if z_max <= z_min:
            raise ValueError("conductor prism must have finite positive thickness")
        object.__setattr__(self, "rings", rings)
        object.__setattr__(self, "z_min", z_min)
        object.__setattr__(self, "z_max", z_max)


@dataclass(frozen=True)
class DielectricBox:
    name: str
    bounds: BoxBounds
    relative_permittivity: float

    def __post_init__(self):
        permittivity = _finite_scalar(
            self.relative_permittivity, label="dielectric permittivity"
        )
        if (not isinstance(self.name, str) or not self.name
                or permittivity <= 0.0):
            raise ValueError("dielectric name and positive permittivity are required")
        object.__setattr__(self, "relative_permittivity", permittivity)


@dataclass(frozen=True)
class DielectricPrism:
    name: str
    rings: tuple[tuple[tuple[float, float], ...], ...]
    z_min: float
    z_max: float
    relative_permittivity: float

    def __post_init__(self):
        prism = ConductorPrism(self.name, self.rings, self.z_min, self.z_max)
        permittivity = _finite_scalar(
            self.relative_permittivity, label="dielectric permittivity"
        )
        if permittivity <= 0.0:
            raise ValueError("dielectric name and positive permittivity are required")
        object.__setattr__(self, "rings", prism.rings)
        object.__setattr__(self, "z_min", prism.z_min)
        object.__setattr__(self, "z_max", prism.z_max)
        object.__setattr__(self, "relative_permittivity", permittivity)


def _prism_bounds(prisms):
    points = [point for prism in prisms for ring in prism.rings for point in ring]
    return BoxBounds(
        (min(point[0] for point in points), min(point[1] for point in points),
         min(prism.z_min for prism in prisms)),
        (max(point[0] for point in points), max(point[1] for point in points),
         max(prism.z_max for prism in prisms)),
    )


def _dielectric_bounds(dielectric):
    if isinstance(dielectric, DielectricBox):
        return dielectric.bounds
    return _prism_bounds((dielectric,))


def _group_items(items):
    grouped = {}
    for item in items:
        grouped.setdefault(item.name, []).append(item)
    return tuple((name, tuple(values)) for name, values in grouped.items())


def _aggregate_bounds(bounds):
    return BoxBounds(
        tuple(min(item.minimum[axis] for item in bounds) for axis in range(3)),
        tuple(max(item.maximum[axis] for item in bounds) for axis in range(3)),
    )


def _mapped_groups(names, mapped_values):
    if len(names) != len(mapped_values):
        raise RuntimeError("Gmsh grouped volume map is inconsistent")
    grouped = {}
    for name, values in zip(names, mapped_values):
        grouped.setdefault(name, set()).update(values)
    return tuple(grouped.items())


def _fuse_named_volumes(gmsh, items, tags):
    grouped = {}
    for item, tag in zip(items, tags):
        grouped.setdefault(item.name, []).append(tag)
    names = []
    fused_tags = []
    for name, values in grouped.items():
        if len(values) == 1:
            outputs = [(3, values[0])]
        else:
            outputs, _ = gmsh.model.occ.fuse(
                [(3, values[0])], [(3, tag) for tag in values[1:]]
            )
        volumes = [tag for dimension, tag in outputs if dimension == 3]
        if not volumes:
            raise RuntimeError("Gmsh lost a grouped volume during union")
        names.extend([name] * len(volumes))
        fused_tags.extend(volumes)
    return tuple(names), tuple(fused_tags)


def validate_palace_mesh_manifest(path, *, mesh_path=None):
    path = Path(path).resolve()
    try:
        raw = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid Palace mesh manifest: {error}") from error
    if isinstance(raw, dict) and raw.get("format") == "dcdc-palace-plc-mesh-v2":
        if __package__:
            from .palace_plc_mesh import validate_palace_plc_mesh_manifest
        else:
            from palace_plc_mesh import validate_palace_plc_mesh_manifest
        return validate_palace_plc_mesh_manifest(path, mesh_path=mesh_path)
    if not isinstance(raw, dict) or raw.get("format") != MESH_MANIFEST_FORMAT:
        raise ValueError("unsupported Palace mesh manifest format")
    provenance = raw.get("provenance")
    if (not isinstance(provenance, dict)
            or raw.get("provenance_sha256") != canonical_sha256(provenance)):
        raise ValueError("Palace mesh provenance hash mismatch")
    if mesh_path is None:
        mesh_path = Path(str(path).removesuffix(".manifest.json"))
    mesh_path = Path(mesh_path).resolve()
    if provenance.get("mesh_sha256") != file_sha256(mesh_path):
        raise ValueError("Palace mesh hash mismatch")
    required = {
        "conductors", "dielectrics", "edge_count", "face_count", "gmsh",
        "ground_attribute", "material_attributes", "mesh_parameters",
        "mesh_sha256", "node_count", "outer_bounds", "outer_permittivity",
        "reference_semantics", "terminal_attributes", "tetrahedron_count",
    }
    if set(provenance) != required:
        raise ValueError("Palace mesh provenance schema mismatch")
    try:
        outer = BoxBounds(**provenance["outer_bounds"])
        conductors = tuple(ConductorPrism(
            item["name"], tuple(tuple(tuple(point) for point in ring)
                                for ring in item["rings"]),
            item["z_min"], item["z_max"],
        ) for item in provenance["conductors"])
        dielectrics = tuple(
            DielectricBox(
                item["name"], BoxBounds(**item["bounds"]),
                item["relative_permittivity"],
            ) if "bounds" in item else DielectricPrism(
                item["name"], tuple(tuple(tuple(point) for point in ring)
                                    for ring in item["rings"]),
                item["z_min"], item["z_max"], item["relative_permittivity"],
            )
            for item in provenance["dielectrics"]
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"invalid Palace mesh geometry: {error}") from error
    if not conductors:
        raise ValueError("Palace mesh requires at least one conductor volume")
    conductor_groups = _group_items(conductors)
    dielectric_groups = _group_items(dielectrics)
    for _, values in dielectric_groups:
        if len({item.relative_permittivity for item in values}) != 1:
            raise ValueError("same-name dielectric volumes must share permittivity")
    for conductor in conductors:
        if not (outer.minimum[2] < conductor.z_min < conductor.z_max < outer.maximum[2]):
            raise ValueError("Palace conductor lies outside the outer domain")
        if any(not (outer.minimum[0] < point[0] < outer.maximum[0]
                    and outer.minimum[1] < point[1] < outer.maximum[1])
               for ring in conductor.rings for point in ring):
            raise ValueError("Palace conductor lies outside the outer domain")
    for dielectric in dielectrics:
        bounds = _dielectric_bounds(dielectric)
        if any(low <= outer_low or high >= outer_high for low, high, outer_low,
               outer_high in zip(bounds.minimum, bounds.maximum,
                                 outer.minimum, outer.maximum)):
            raise ValueError("Palace dielectric lies outside the outer domain")
    outer_permittivity = provenance["outer_permittivity"]
    if (isinstance(outer_permittivity, bool)
            or not isinstance(outer_permittivity, (int, float))
            or not np.isfinite(outer_permittivity) or outer_permittivity <= 0.0):
        raise ValueError("Palace outer permittivity must be finite and positive")
    terminal_attributes = provenance["terminal_attributes"]
    material_attributes = provenance["material_attributes"]
    if (not isinstance(terminal_attributes, list)
            or len(terminal_attributes) != len(conductor_groups)
            or any(not isinstance(item, list) or len(item) != 2
                   or not isinstance(item[0], str) or type(item[1]) is not int
                   or item[1] <= 0 for item in terminal_attributes)
            or [item[0] for item in terminal_attributes]
            != [name for name, _ in conductor_groups]
            or len({item[1] for item in terminal_attributes})
            != len(terminal_attributes)):
        raise ValueError("Palace terminal attributes are invalid")
    expected_material_names = ["outer", *(name for name, _ in dielectric_groups)]
    if (not isinstance(material_attributes, list)
            or len(material_attributes) != len(expected_material_names)
            or any(not isinstance(item, list) or len(item) != 3
                   or not isinstance(item[0], str) or type(item[1]) is not int
                   or item[1] <= 0 or isinstance(item[2], bool)
                   or not isinstance(item[2], (int, float))
                   or not np.isfinite(item[2]) or item[2] <= 0.0
                   for item in material_attributes)
            or [item[0] for item in material_attributes] != expected_material_names
            or len({item[1] for item in material_attributes})
            != len(material_attributes)):
        raise ValueError("Palace material attributes are invalid")
    if not canonical_equal(material_attributes[0][2], outer_permittivity):
        raise ValueError("Palace outer material permittivity is inconsistent")
    ground_attribute = provenance["ground_attribute"]
    occupied_attributes = {
        *(item[1] for item in terminal_attributes),
        *(item[1] for item in material_attributes),
    }
    if (type(ground_attribute) is not int or ground_attribute <= 0
            or ground_attribute in occupied_attributes):
        raise ValueError("Palace ground attribute is invalid")
    reference = provenance["reference_semantics"]
    if not canonical_equal(reference, {
        "kind": "finite_outer_dirichlet_approximation",
        "convergence_ladder_required": True,
        "not_a_circuit_node": True,
    }):
        raise ValueError("Palace mesh reference semantics are not qualification-safe")
    parameters = provenance["mesh_parameters"]
    if (not isinstance(parameters, dict) or set(parameters) != {
            "algorithm_3d", "mesh_size_far_m", "mesh_size_near_m", "msh_version",
            "threads", "transition_distance_m"}
            or type(parameters["algorithm_3d"]) is not int
            or parameters["algorithm_3d"] not in (1, 10)
            or type(parameters["threads"]) is not int or parameters["threads"] != 1
            or isinstance(parameters["msh_version"], bool)
            or not isinstance(parameters["msh_version"], (int, float))
            or parameters["msh_version"] != 2.2
            or any(isinstance(parameters[name], bool)
                   or not isinstance(parameters[name], (int, float))
                   or not np.isfinite(parameters[name]) or parameters[name] <= 0.0
                   for name in ("mesh_size_far_m", "mesh_size_near_m",
                                "transition_distance_m"))):
        raise ValueError("Palace mesh parameters are not qualification-safe")
    gmsh = provenance["gmsh"]
    if (not isinstance(gmsh, dict) or set(gmsh) != {
            "version", "python_module", "python_module_sha256", "library",
            "library_sha256"} or not isinstance(gmsh["version"], str)
            or not gmsh["version"]):
        raise ValueError("Palace Gmsh identity is invalid")
    for file_key, hash_key in (
            ("python_module", "python_module_sha256"),
            ("library", "library_sha256")):
        if file_sha256(gmsh[file_key]) != gmsh[hash_key]:
            raise ValueError(f"Palace Gmsh {file_key} hash mismatch")
    count_names = (
        "node_count", "edge_count", "face_count", "tetrahedron_count"
    )
    for name in count_names:
        if type(provenance[name]) is not int or provenance[name] <= 0:
            raise ValueError("Palace mesh counts must be positive integers")
    actual_counts = tetrahedral_mesh_counts(mesh_path)
    recorded_counts = tuple(provenance[name] for name in count_names)
    if recorded_counts != actual_counts:
        raise ValueError("Palace mesh topology counts differ from mesh bytes")
    return raw


def validate_palace_mesh_content(mesh_path, provenance):
    import gmsh

    mesh_path = Path(mesh_path).resolve()
    expected_groups = {}
    outer = BoxBounds(**provenance["outer_bounds"])
    expected_groups[(3, provenance["material_attributes"][0][1])] = (
        provenance["material_attributes"][0][0], outer,
    )
    dielectric_records = []
    for item in provenance["dielectrics"]:
        dielectric_records.append(
            DielectricBox(
                item["name"], BoxBounds(**item["bounds"]),
                item["relative_permittivity"],
            ) if "bounds" in item else DielectricPrism(
                item["name"], tuple(tuple(tuple(point) for point in ring)
                                    for ring in item["rings"]),
                item["z_min"], item["z_max"], item["relative_permittivity"],
            )
        )
    for (name, values), material in zip(
            _group_items(dielectric_records),
            provenance["material_attributes"][1:]):
        bounds = _aggregate_bounds(tuple(_dielectric_bounds(item) for item in values))
        expected_groups[(3, material[1])] = (name, bounds)
    conductor_records = tuple(ConductorPrism(
        item["name"], tuple(tuple(tuple(point) for point in ring)
                            for ring in item["rings"]),
        item["z_min"], item["z_max"],
    ) for item in provenance["conductors"])
    for (name, values), terminal in zip(
            _group_items(conductor_records), provenance["terminal_attributes"]):
        expected_groups[(2, terminal[1])] = (name, _prism_bounds(values))
    expected_groups[(2, provenance["ground_attribute"])] = (
        "electrostatic_infinity_boundary", outer,
    )
    initialized_here = not gmsh.isInitialized()
    if initialized_here:
        gmsh.initialize()
    try:
        gmsh.clear()
        gmsh.open(str(mesh_path))
        groups = set(gmsh.model.getPhysicalGroups())
        if groups != set(expected_groups):
            raise ValueError("Palace mesh physical groups differ from provenance")
        for key, (expected_name, expected_bounds) in expected_groups.items():
            dimension, attribute = key
            if gmsh.model.getPhysicalName(dimension, attribute) != expected_name:
                raise ValueError("Palace mesh physical-group name differs from provenance")
            entities = gmsh.model.getEntitiesForPhysicalGroup(dimension, attribute)
            boxes = [gmsh.model.getBoundingBox(dimension, entity) for entity in entities]
            actual = (
                tuple(min(box[axis] for box in boxes) for axis in range(3)),
                tuple(max(box[axis] for box in boxes) for axis in range(3, 6)),
            )
            expected = (expected_bounds.minimum, expected_bounds.maximum)
            if not np.allclose(actual, expected, rtol=1e-10, atol=1e-12):
                raise ValueError(
                    "Palace mesh physical-group bounds differ from provenance: "
                    f"group={key}, actual={actual}, expected={expected}"
                )
        counts = _active_tetrahedral_mesh_counts(gmsh)
        expected_counts = tuple(provenance[name] for name in (
            "node_count", "edge_count", "face_count", "tetrahedron_count"
        ))
        if counts != expected_counts:
            raise ValueError("Palace mesh counts differ from provenance")
    except Exception as error:
        if isinstance(error, ValueError):
            raise
        raise ValueError(f"invalid Palace mesh content: {error}") from error
    finally:
        gmsh.clear()
        if initialized_here:
            gmsh.finalize()


@dataclass(frozen=True)
class PalaceMeshResult:
    mesh_path: Path
    manifest_path: Path
    terminal_attributes: tuple[tuple[str, int], ...]
    material_attributes: tuple[tuple[str, int, float], ...]
    ground_attribute: int
    node_count: int
    tetrahedron_count: int


def _add_box(gmsh, bounds):
    return gmsh.model.occ.addBox(*bounds.minimum, *bounds.lengths)


def _add_prism(gmsh, prism):
    occ = gmsh.model.occ
    loops = []
    for ring in prism.rings:
        points = [occ.addPoint(x, y, prism.z_min) for x, y in ring]
        lines = [
            occ.addLine(points[index], points[(index + 1) % len(points)])
            for index in range(len(points))
        ]
        loops.append(occ.addCurveLoop(lines))
    surface = occ.addPlaneSurface(loops)
    entities = occ.extrude([(2, surface)], 0.0, 0.0, prism.z_max - prism.z_min)
    volumes = [tag for dimension, tag in entities if dimension == 3]
    if len(volumes) != 1:
        raise RuntimeError("Gmsh did not create exactly one prism volume")
    return volumes[0]


def _add_dielectric(gmsh, dielectric):
    if isinstance(dielectric, DielectricBox):
        return _add_box(gmsh, dielectric.bounds)
    return _add_prism(gmsh, dielectric)


def _surface_tags(gmsh, volumes):
    tags = []
    for volume in volumes:
        tags.extend(
            tag for dimension, tag in gmsh.model.getBoundary(
                [(3, volume)], combined=False, oriented=False
            ) if dimension == 2
        )
    return tuple(sorted(set(tags)))


def _interface_surfaces(gmsh, volumes, conductor_union):
    selected = []
    for surface in _surface_tags(gmsh, volumes):
        upward, _ = gmsh.model.getAdjacencies(2, surface)
        if any(int(volume) not in conductor_union for volume in upward):
            selected.append(surface)
    return tuple(sorted(set(selected)))


def _outer_surfaces(gmsh, volume_tags, outer, tolerance):
    surfaces = _surface_tags(gmsh, volume_tags)
    selected = []
    for surface in surfaces:
        center = gmsh.model.occ.getCenterOfMass(2, surface)
        if any(
            abs(center[axis] - outer.minimum[axis]) <= tolerance
            or abs(center[axis] - outer.maximum[axis]) <= tolerance
            for axis in range(3)
        ):
            selected.append(surface)
    return tuple(sorted(set(selected)))


def _gmsh_identity(gmsh):
    module_path = Path(gmsh.__file__).resolve()
    identity = {
        "version": gmsh.__version__,
        "python_module": str(module_path),
        "python_module_sha256": file_sha256(module_path),
    }
    roots = (module_path.parent, *tuple(module_path.parents)[:4])
    libraries = sorted({
        candidate.resolve()
        for root in roots
        for pattern in ("libgmsh*.dylib", "libgmsh*.so", "gmsh*.dll")
        for candidate in root.glob(pattern)
        if candidate.is_file()
    })
    if len(libraries) != 1:
        raise RuntimeError("Gmsh native library identity is missing or ambiguous")
    identity["library"] = str(libraries[0])
    identity["library_sha256"] = file_sha256(libraries[0])
    return identity


def generate_palace_mesh(path, *, outer_bounds, conductors, dielectrics=(),
                         outer_permittivity=1.0, mesh_size_near_m,
                         mesh_size_far_m, transition_distance_m,
                         algorithm_3d=10):
    try:
        import gmsh
    except ImportError as error:
        raise RuntimeError("Gmsh Python bindings are required for Palace meshing") from error

    path = Path(path).resolve()
    outer_bounds = BoxBounds(outer_bounds.minimum, outer_bounds.maximum)
    conductors = tuple(conductors)
    dielectrics = tuple(dielectrics)
    outer_permittivity = float(outer_permittivity)
    mesh_size_near_m = float(mesh_size_near_m)
    mesh_size_far_m = float(mesh_size_far_m)
    transition_distance_m = float(transition_distance_m)
    if type(algorithm_3d) is not int or algorithm_3d not in (1, 10):
        raise ValueError("unsupported Gmsh 3D mesh algorithm")
    if not conductors:
        raise ValueError("at least one conductor is required")
    conductor_groups = _group_items(conductors)
    dielectric_groups = _group_items(dielectrics)
    for _, values in dielectric_groups:
        if len({item.relative_permittivity for item in values}) != 1:
            raise ValueError("same-name dielectric volumes must share permittivity")
    if (not np.isfinite((outer_permittivity, mesh_size_near_m, mesh_size_far_m,
                         transition_distance_m)).all()
            or outer_permittivity <= 0.0 or mesh_size_near_m <= 0.0
            or mesh_size_far_m < mesh_size_near_m or transition_distance_m <= 0.0):
        raise ValueError("mesh scales and outer permittivity must be finite and positive")
    for conductor in conductors:
        if (conductor.z_min <= outer_bounds.minimum[2]
                or conductor.z_max >= outer_bounds.maximum[2]):
            raise ValueError("conductors must lie strictly inside the outer domain")
        for ring in conductor.rings:
            if any(
                not (outer_bounds.minimum[0] < x < outer_bounds.maximum[0]
                     and outer_bounds.minimum[1] < y < outer_bounds.maximum[1])
                for x, y in ring
            ):
                raise ValueError("conductors must lie strictly inside the outer domain")

    for dielectric in dielectrics:
        bounds = _dielectric_bounds(dielectric)
        if any(low <= outer_low or high >= outer_high for low, high, outer_low,
               outer_high in zip(bounds.minimum, bounds.maximum,
                                 outer_bounds.minimum, outer_bounds.maximum)):
            raise ValueError("dielectrics must lie strictly inside the outer domain")

    path.parent.mkdir(parents=True, exist_ok=True)
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("General.NumThreads", 1)
        gmsh.option.setNumber("Mesh.MaxNumThreads1D", 1)
        gmsh.option.setNumber("Mesh.MaxNumThreads2D", 1)
        gmsh.option.setNumber("Mesh.MaxNumThreads3D", 1)
        gmsh.option.setNumber("Mesh.Binary", 0)
        gmsh.option.setNumber("Mesh.MshFileVersion", 2.2)
        gmsh.model.add(path.stem)
        outer_input = _add_box(gmsh, outer_bounds)
        dielectric_inputs = [_add_dielectric(gmsh, item) for item in dielectrics]
        conductor_inputs = [_add_prism(gmsh, item) for item in conductors]
        dielectric_names, dielectric_inputs = _fuse_named_volumes(
            gmsh, dielectrics, dielectric_inputs
        )
        conductor_names, conductor_inputs = _fuse_named_volumes(
            gmsh, conductors, conductor_inputs
        )
        inputs = [(3, outer_input)] + [
            (3, tag) for tag in (*dielectric_inputs, *conductor_inputs)
        ]
        _, mappings = gmsh.model.occ.fragment(inputs[:1], inputs[1:])
        gmsh.model.occ.synchronize()
        if len(mappings) != len(inputs):
            raise RuntimeError("Gmsh fragment map does not cover every input volume")

        mapped = [
            {tag for dimension, tag in entries if dimension == 3}
            for entries in mappings
        ]
        all_volumes = mapped[0]
        dielectric_parts = mapped[1:1 + len(dielectric_inputs)]
        conductor_parts = mapped[1 + len(dielectric_inputs):]
        dielectric_volume_groups = _mapped_groups(
            dielectric_names, dielectric_parts
        )
        conductor_volume_groups = _mapped_groups(
            conductor_names, conductor_parts
        )
        if any(not values for _, values in conductor_volume_groups):
            raise RuntimeError("Gmsh lost a conductor during volume fragmentation")
        conductor_union = set().union(
            *(values for _, values in conductor_volume_groups)
        )
        conductor_overlaps = [
            (left_name, right_name, sorted(left & right))
            for index, (left_name, left) in enumerate(conductor_volume_groups)
            for right_name, right in conductor_volume_groups[index + 1:]
            if left & right
        ]
        if conductor_overlaps:
            raise ValueError(
                f"different conductor groups overlap: {conductor_overlaps}"
            )
        if any(left & right
               for index, (_, left) in enumerate(dielectric_volume_groups)
               for _, right in dielectric_volume_groups[index + 1:]):
            raise ValueError("different dielectric groups overlap")

        terminal_surfaces = [
            _interface_surfaces(gmsh, volumes, conductor_union)
            for _, volumes in conductor_volume_groups
        ]
        gmsh.model.occ.remove(
            [(3, tag) for tag in sorted(conductor_union)], recursive=False
        )
        gmsh.model.occ.synchronize()
        domain_volumes = set(all_volumes) - conductor_union
        material_volume_sets = []
        occupied = set(conductor_union)
        for _, values in reversed(dielectric_volume_groups):
            material_volume_sets.append(set(values) - occupied)
            occupied.update(values)
        material_volume_sets.reverse()
        dielectric_union = set().union(
            *(values for _, values in dielectric_volume_groups)
        ) if dielectric_volume_groups else set()
        outer_volumes = domain_volumes - dielectric_union - conductor_union
        if not outer_volumes or any(not values for values in material_volume_sets):
            raise RuntimeError("Gmsh material partition is incomplete")

        material_attributes = [("outer", 1, outer_permittivity)]
        gmsh.model.addPhysicalGroup(3, sorted(outer_volumes), 1)
        gmsh.model.setPhysicalName(3, 1, "outer")
        for offset, ((name, dielectric_parts), volumes) in enumerate(
                zip(dielectric_groups, material_volume_sets), 2):
            gmsh.model.addPhysicalGroup(3, sorted(volumes), offset)
            gmsh.model.setPhysicalName(3, offset, name)
            material_attributes.append(
                (name, offset, dielectric_parts[0].relative_permittivity)
            )

        terminal_attributes = []
        for index, ((name, _), surfaces) in enumerate(
                zip(conductor_groups, terminal_surfaces), 1):
            if not surfaces:
                raise RuntimeError("conductor group has no material interface")
            attribute = 100 + index
            gmsh.model.addPhysicalGroup(2, list(surfaces), attribute)
            gmsh.model.setPhysicalName(2, attribute, name)
            terminal_attributes.append((name, attribute))
        tolerance = 1e-8 * max(outer_bounds.lengths)
        ground_surfaces = _outer_surfaces(
            gmsh, sorted(domain_volumes), outer_bounds, tolerance
        )
        if len(ground_surfaces) != 6:
            raise RuntimeError("outer Dirichlet boundary must contain exactly six box faces")
        ground_attribute = 9999
        gmsh.model.addPhysicalGroup(2, list(ground_surfaces), ground_attribute)
        gmsh.model.setPhysicalName(2, ground_attribute, "electrostatic_infinity_boundary")

        field = gmsh.model.mesh.field
        distance = field.add("Distance")
        field.setNumbers(
            distance, "SurfacesList", sorted(set().union(*map(set, terminal_surfaces)))
        )
        field.setNumber(distance, "Sampling", 100)
        threshold = field.add("Threshold")
        field.setNumber(threshold, "InField", distance)
        field.setNumber(threshold, "SizeMin", mesh_size_near_m)
        field.setNumber(threshold, "SizeMax", mesh_size_far_m)
        field.setNumber(threshold, "DistMin", mesh_size_near_m)
        field.setNumber(threshold, "DistMax", transition_distance_m)
        field.setAsBackgroundMesh(threshold)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 0)
        gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 0)
        gmsh.option.setNumber("Mesh.Algorithm3D", algorithm_3d)
        gmsh.model.mesh.generate(3)
        gmsh.write(str(path))
        node_count, edge_count, face_count, tetrahedron_count = (
            _active_tetrahedral_mesh_counts(gmsh)
        )
        gmsh_identity = _gmsh_identity(gmsh)
    finally:
        gmsh.finalize()

    provenance = {
        "outer_bounds": asdict(outer_bounds),
        "outer_permittivity": outer_permittivity,
        "conductors": [asdict(item) for item in conductors],
        "dielectrics": [asdict(item) for item in dielectrics],
        "terminal_attributes": terminal_attributes,
        "material_attributes": material_attributes,
        "ground_attribute": ground_attribute,
        "reference_semantics": {
            "kind": "finite_outer_dirichlet_approximation",
            "convergence_ladder_required": True,
            "not_a_circuit_node": True,
        },
        "mesh_parameters": {
            "mesh_size_near_m": mesh_size_near_m,
            "mesh_size_far_m": mesh_size_far_m,
            "transition_distance_m": transition_distance_m,
            "algorithm_3d": algorithm_3d,
            "threads": 1,
            "msh_version": 2.2,
        },
        "node_count": node_count,
        "edge_count": edge_count,
        "face_count": face_count,
        "tetrahedron_count": tetrahedron_count,
        "gmsh": gmsh_identity,
        "mesh_sha256": file_sha256(path),
    }
    manifest = {
        "format": MESH_MANIFEST_FORMAT,
        "provenance": provenance,
        "provenance_sha256": canonical_sha256(provenance),
    }
    manifest_path = path.with_suffix(path.suffix + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return PalaceMeshResult(
        mesh_path=path,
        manifest_path=manifest_path,
        terminal_attributes=tuple(terminal_attributes),
        material_attributes=tuple(material_attributes),
        ground_attribute=ground_attribute,
        node_count=node_count,
        tetrahedron_count=tetrahedron_count,
    )
