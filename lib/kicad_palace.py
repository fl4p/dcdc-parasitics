#!/usr/bin/env python3
"""Convert a complete KiCad PCB-volume dump into Palace volume primitives."""
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path

from shapely import make_valid, set_precision
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from kicad_fastercap import (
    KICAD_GRID_MM,
    MM_TO_M,
    _boundary_segment_lengths,
    _polygonal_parts,
    copper_layer_bounds,
)
from kicad_palace_schema import (
    PCB_VOLUME_DUMP_FORMAT, check_board_outline_fill,
    outline_bounding_area_mm2,
)
from palace_mesh import BoxBounds, ConductorPrism, DielectricPrism
from provenance import canonical_sha256, file_sha256


VOLUME_GRID_MM = KICAD_GRID_MM * 1e-3


@dataclass(frozen=True)
class PalaceBoardGeometry:
    conductors: tuple[ConductorPrism, ...]
    dielectrics: tuple[DielectricPrism, ...]
    outer_bounds: BoxBounds
    groups: tuple[str, ...]
    dump_sha256: str
    dump_content_sha256: str
    census: dict
    plating_thickness_m: float
    geometry_tolerance_mm: float
    coordinate_grid_mm: float
    simplification_area_rtol: float
    outer_scale: float
    drill_circle_points: int
    material_permittivity_overrides: tuple[tuple[str, float], ...]


def load_pcb_volume_dump(path):
    path = Path(path).resolve()
    try:
        dump = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid KiCad Palace dump: {error}") from error
    if not isinstance(dump, dict) or dump.get("format") != PCB_VOLUME_DUMP_FORMAT:
        raise ValueError("unsupported KiCad Palace dump format")
    required = {
        "board_outlines", "census", "copper_layers", "drills", "format",
        "grouping_policy", "groups", "kicad_version", "polygon_error_mm",
        "python_executable", "records", "source_pcb_path", "source_pcb_sha256",
    }
    if set(dump) != required:
        raise ValueError("KiCad Palace dump schema mismatch")
    if (not isinstance(dump["groups"], list) or not dump["groups"]
            or len(set(dump["groups"])) != len(dump["groups"])
            or not isinstance(dump["records"], list)
            or not isinstance(dump["drills"], list)
            or not isinstance(dump["board_outlines"], list)
            or not dump["board_outlines"] or not isinstance(dump["census"], dict)
            or dump["grouping_policy"] not in (
                "explicit", "all_nets_and_isolated_items")
            or not isinstance(dump["source_pcb_path"], str)
            or not isinstance(dump["source_pcb_sha256"], str)
            or len(dump["source_pcb_sha256"]) != 64):
        raise ValueError("KiCad Palace dump containers are invalid")
    # Dumps written before the outline fix hold a stroked Edge.Cuts frame rather
    # than the board region, and load without complaint: the geometry is valid,
    # merely 99.5% missing. Checking the producer alone would leave every such
    # dump on disk silently reproducing its old numbers, so this is checked on
    # the way in as well, against the outline's own extent.
    check_board_outline_fill(
        dump["board_outlines"],
        outline_bounding_area_mm2(dump["board_outlines"]),
    )
    return dump


def _json_content_sha256(value):
    return canonical_sha256(value)


def _drill_polygon(center, size, shape, points, *, expansion=0.0):
    if shape == "circle":
        if not math.isclose(size[0], size[1], rel_tol=0.0, abs_tol=KICAD_GRID_MM):
            raise ValueError("round drill must have equal width and height")
        radius = 0.5 * size[0] + expansion
        if radius <= 0.0:
            raise ValueError("round drill must have positive radius")
        ring = tuple(
            (
                round((center[0] + radius * math.cos(
                    2.0 * math.pi * index / points)) / KICAD_GRID_MM)
                * KICAD_GRID_MM,
                round((center[1] + radius * math.sin(
                    2.0 * math.pi * index / points)) / KICAD_GRID_MM)
                * KICAD_GRID_MM,
            )
            for index in range(points)
        )
        return Polygon(ring)
    if shape != "oblong":
        raise ValueError(f"unsupported drill shape {shape!r}")
    width, height = size
    radius = 0.5 * min(width, height) + expansion
    half_segment = 0.5 * abs(width - height)
    if radius <= 0.0 or half_segment <= 0.0:
        raise ValueError("oblong drill must have unequal positive dimensions")
    if width > height:
        endpoints = ((center[0] - half_segment, center[1]),
                     (center[0] + half_segment, center[1]))
    else:
        endpoints = ((center[0], center[1] - half_segment),
                     (center[0], center[1] + half_segment))
    return set_precision(
        LineString(endpoints).buffer(radius, quad_segs=points // 4),
        KICAD_GRID_MM,
        mode="valid_output",
    )


def _rings_m(polygon):
    return (
        tuple((x * MM_TO_M, y * MM_TO_M)
              for x, y in tuple(polygon.exterior.coords)[:-1]),
        *(tuple((x * MM_TO_M, y * MM_TO_M)
                for x, y in tuple(interior.coords)[:-1])
          for interior in polygon.interiors),
    )


def _raw_record_geometry(record):
    polygon = Polygon(record["shell"], record.get("holes", ()))
    if polygon.is_valid:
        return polygon
    repaired = make_valid(polygon)
    parts = _polygonal_parts(repaired)
    repaired_area = sum(part.area for part in parts)
    tolerance = max(1e-12, 1e-9 * polygon.area)
    if not parts or abs(repaired_area - polygon.area) > tolerance:
        raise ValueError("repairing a KiCad copper contour changed its area")
    return unary_union(parts)


def _volume_grid(tolerance_mm):
    return max(VOLUME_GRID_MM, 0.5 * tolerance_mm)


def _condition_volume_geometry(geometry, tolerance_mm, area_rtol):
    original_area = float(geometry.area)
    conditioned = geometry.simplify(tolerance_mm, preserve_topology=True)
    conditioned = set_precision(
        conditioned, _volume_grid(tolerance_mm), mode="valid_output"
    )
    area_error = float(conditioned.area) - original_area
    allowed = max(1e-10, area_rtol * original_area)
    if abs(area_error) > allowed:
        raise ValueError(
            "geometry simplification changed copper area beyond its gate: "
            f"{area_error:g} mm^2 > {allowed:g} mm^2"
        )
    lengths = _boundary_segment_lengths(conditioned)
    if not lengths or min(lengths) < tolerance_mm * 0.5:
        raise ValueError("conditioned copper contains a sub-tolerance segment")
    return conditioned


def _condition_records(records, *, tolerance_mm, area_rtol, cutouts=()):
    geometry = unary_union(tuple(_raw_record_geometry(record) for record in records))
    if geometry.is_empty or not geometry.is_valid:
        raise ValueError("KiCad copper union is empty or invalid")
    geometry = _condition_volume_geometry(geometry, tolerance_mm, area_rtol)
    if cutouts:
        geometry = set_precision(
            geometry.difference(unary_union(tuple(cutouts))),
            _volume_grid(tolerance_mm),
            mode="valid_output",
        )
    if geometry.is_empty or not geometry.is_valid:
        raise ValueError("drilled KiCad copper is empty or invalid")
    return tuple(_polygonal_parts(geometry))


def _drill_layer_bounds(drill, copper_bounds):
    try:
        return (
            copper_bounds[drill["start_layer"]],
            copper_bounds[drill["end_layer"]],
        )
    except KeyError as error:
        raise ValueError(f"drill references unknown copper layer {error}") from error


def _drill_z_bounds(drill, copper_bounds):
    start, end = _drill_layer_bounds(drill, copper_bounds)
    return min(*start, *end), max(*start, *end)


def _barrel_z_bounds(drill, copper_bounds):
    start, end = sorted(
        _drill_layer_bounds(drill, copper_bounds), key=lambda item: sum(item)
    )
    if start == end:
        raise ValueError("plated drill must span distinct copper layers")
    return start[1], end[0]


def _dielectric_z_bounds(stackup, index):
    layer = stackup[index]
    z_top = layer.z_top_mm
    z_bottom = layer.z_bottom_mm
    if index > 0 and stackup[index - 1].kind.lower() == "copper":
        copper = stackup[index - 1]
        has_material_above = any(
            item.kind.lower() != "copper" and item.thickness_mm > 0.0
            for item in stackup[:index - 1]
        )
        z_top = (
            0.5 * (copper.z_top_mm + copper.z_bottom_mm)
            if has_material_above else copper.z_top_mm
        )
    if index + 1 < len(stackup) and stackup[index + 1].kind.lower() == "copper":
        copper = stackup[index + 1]
        has_material_below = any(
            item.kind.lower() != "copper" and item.thickness_mm > 0.0
            for item in stackup[index + 2:]
        )
        z_bottom = (
            0.5 * (copper.z_top_mm + copper.z_bottom_mm)
            if has_material_below else copper.z_bottom_mm
        )
    return z_bottom, z_top


def _material_permittivity(layer, overrides):
    for key in (layer.name, layer.kind):
        if key in overrides:
            value = overrides[key]
            break
    else:
        value = layer.epsilon_r
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0.0):
        raise ValueError(
            f"dielectric layer {layer.name!r} has no positive permittivity"
        )
    return float(value)


def _complete_census(dump):
    census = dump["census"]
    if set(census) != {"included", "unassigned", "unsupported"}:
        raise ValueError("KiCad Palace census schema mismatch")
    if not isinstance(census["unassigned"], list) or not isinstance(
            census["unsupported"], list):
        raise ValueError("KiCad Palace census containers are invalid")
    failures = []
    if census["unassigned"]:
        failures.append(f"{len(census['unassigned'])} unassigned copper items")
    if census["unsupported"]:
        failures.append(f"{len(census['unsupported'])} unsupported copper features")
    if failures:
        raise ValueError("incomplete KiCad Palace geometry: " + "; ".join(failures))


def volumes_from_pcb_dump(
        dump, stackup, *, plating_thickness_m, material_permittivity=None,
        geometry_tolerance_mm=1e-3, simplification_area_rtol=1e-4,
        outer_scale=2.0, drill_circle_points=64):
    if dump.get("format") != PCB_VOLUME_DUMP_FORMAT:
        raise ValueError("unsupported KiCad Palace dump format")
    _complete_census(dump)
    groups = tuple(dump["groups"])
    if not groups or len(set(groups)) != len(groups):
        raise ValueError("KiCad Palace groups must be non-empty and unique")
    plating_thickness_m = float(plating_thickness_m)
    geometry_tolerance_mm = float(geometry_tolerance_mm)
    simplification_area_rtol = float(simplification_area_rtol)
    outer_scale = float(outer_scale)
    if (not math.isfinite(plating_thickness_m) or plating_thickness_m <= 0.0
            or not math.isfinite(geometry_tolerance_mm)
            or geometry_tolerance_mm <= 0.0
            or not math.isfinite(simplification_area_rtol)
            or simplification_area_rtol < 0.0
            or not math.isfinite(outer_scale) or outer_scale <= 1.0
            or type(drill_circle_points) is not int or drill_circle_points < 16):
        raise ValueError("KiCad Palace geometry controls are invalid")
    material_permittivity = dict(material_permittivity or {})
    bounds = copper_layer_bounds(stackup)
    if set(dump["copper_layers"]) != set(bounds):
        raise ValueError("KiCad dump copper layers do not match the board stackup")

    working_grid = _volume_grid(geometry_tolerance_mm)
    drill_geometries = []
    for drill in dump["drills"]:
        group = drill.get("group")
        if (drill.get("unsupported_features")
                or (drill.get("plated") and group not in groups)
                or (not drill.get("plated") and group is not None
                    and group not in groups)):
            raise ValueError("KiCad drill identity or feature is unsupported")
        center = tuple(float(value) for value in drill["center_mm"])
        size = tuple(float(value) for value in drill["size_mm"])
        if len(center) != 2 or len(size) != 2:
            raise ValueError("KiCad drill geometry must be two-dimensional")
        z_min_mm, z_max_mm = _drill_z_bounds(drill, bounds)
        inner_hole = set_precision(
            _drill_polygon(center, size, drill.get("shape"), drill_circle_points),
            working_grid,
            mode="valid_output",
        )
        expansion_mm = (
            plating_thickness_m / MM_TO_M if drill["plated"] else 0.0
        )
        material_hole = set_precision(
            _drill_polygon(
                center, size, drill.get("shape"), drill_circle_points,
                expansion=expansion_mm,
            ),
            working_grid,
            mode="valid_output",
        )
        drill_geometries.append({
            "drill": drill,
            "inner_hole": inner_hole,
            "material_hole": material_hole,
            "z_min_mm": z_min_mm,
            "z_max_mm": z_max_mm,
        })

    records = {}
    for record in dump["records"]:
        group = record.get("group")
        layer = record.get("layer")
        if group not in groups or layer not in bounds:
            raise ValueError("KiCad copper record has unknown group or layer")
        records.setdefault((group, layer), []).append(record)
    conductors = []
    for (group, layer), layer_records in records.items():
        z_min_mm, z_max_mm = bounds[layer]
        cutouts = [
            item["inner_hole"] for item in drill_geometries
            if item["z_min_mm"] < z_max_mm and item["z_max_mm"] > z_min_mm
        ]
        for polygon in _condition_records(
                layer_records, tolerance_mm=geometry_tolerance_mm,
                area_rtol=simplification_area_rtol, cutouts=cutouts):
            conductors.append(ConductorPrism(
                group,
                _rings_m(polygon),
                z_min_mm * MM_TO_M,
                z_max_mm * MM_TO_M,
            ))

    for item in drill_geometries:
        drill = item["drill"]
        if drill["plated"]:
            barrel = Polygon(
                item["material_hole"].exterior.coords,
                (item["inner_hole"].exterior.coords,),
            )
            barrel_z_min_mm, barrel_z_max_mm = _barrel_z_bounds(drill, bounds)
            conductors.append(ConductorPrism(
                drill["group"], _rings_m(barrel),
                barrel_z_min_mm * MM_TO_M, barrel_z_max_mm * MM_TO_M,
            ))
    present_groups = {item.name for item in conductors}
    missing = [group for group in groups if group not in present_groups]
    if missing:
        raise ValueError("no conductor volume found for groups: " + ", ".join(missing))

    outline = set_precision(
        unary_union(tuple(
            _raw_record_geometry(record) for record in dump["board_outlines"]
        )),
        working_grid,
        mode="valid_output",
    )
    if outline.is_empty or not outline.is_valid:
        raise ValueError("KiCad board outline is empty or invalid")
    dielectrics = []
    for index, layer in enumerate(stackup):
        if layer.kind.lower() == "copper" or layer.thickness_mm <= 0.0:
            continue
        permittivity = _material_permittivity(layer, material_permittivity)
        z_bottom_mm, z_top_mm = _dielectric_z_bounds(stackup, index)
        layer_geometry = outline
        holes = [
            item["material_hole"] for item in drill_geometries
            if item["z_min_mm"] < z_top_mm
            and item["z_max_mm"] > z_bottom_mm
        ]
        if holes:
            layer_geometry = set_precision(
                layer_geometry.difference(unary_union(holes)),
                working_grid,
                mode="valid_output",
            )
        if layer_geometry.is_empty or not layer_geometry.is_valid:
            raise ValueError(f"dielectric layer {layer.name!r} is empty or invalid")
        for polygon in _polygonal_parts(layer_geometry):
            dielectrics.append(DielectricPrism(
                layer.name,
                _rings_m(polygon),
                z_bottom_mm * MM_TO_M,
                z_top_mm * MM_TO_M,
                permittivity,
            ))

    planar_points = [
        point for prism in (*conductors, *dielectrics)
        for ring in prism.rings for point in ring
    ]
    z_values = [
        value for prism in (*conductors, *dielectrics)
        for value in (prism.z_min, prism.z_max)
    ]
    minimum = (min(point[0] for point in planar_points),
               min(point[1] for point in planar_points), min(z_values))
    maximum = (max(point[0] for point in planar_points),
               max(point[1] for point in planar_points), max(z_values))
    span = max(high - low for low, high in zip(minimum, maximum))
    center = tuple(0.5 * (low + high) for low, high in zip(minimum, maximum))
    half = 0.5 * span * outer_scale
    raw_minimum = tuple(value - half for value in center)
    raw_maximum = tuple(value + half for value in center)
    planar_grid_m = _volume_grid(geometry_tolerance_mm) * MM_TO_M
    outer_bounds = BoxBounds(
        (
            math.floor(raw_minimum[0] / planar_grid_m) * planar_grid_m,
            math.floor(raw_minimum[1] / planar_grid_m) * planar_grid_m,
            raw_minimum[2],
        ),
        (
            math.ceil(raw_maximum[0] / planar_grid_m) * planar_grid_m,
            math.ceil(raw_maximum[1] / planar_grid_m) * planar_grid_m,
            raw_maximum[2],
        ),
    )
    return PalaceBoardGeometry(
        conductors=tuple(conductors),
        dielectrics=tuple(dielectrics),
        outer_bounds=outer_bounds,
        groups=groups,
        dump_sha256="",
        dump_content_sha256=_json_content_sha256(dump),
        census=dump["census"],
        plating_thickness_m=plating_thickness_m,
        geometry_tolerance_mm=geometry_tolerance_mm,
        coordinate_grid_mm=_volume_grid(geometry_tolerance_mm),
        simplification_area_rtol=simplification_area_rtol,
        outer_scale=outer_scale,
        drill_circle_points=drill_circle_points,
        material_permittivity_overrides=tuple(sorted(
            (str(name), float(value))
            for name, value in material_permittivity.items()
        )),
    )


def pcb_volume_source_identity(geometry, dump_path, pcb_path, stackup):
    dump_path = Path(dump_path).resolve()
    pcb_path = Path(pcb_path).resolve()
    dump = load_pcb_volume_dump(dump_path)
    if geometry.dump_sha256 != file_sha256(dump_path):
        raise ValueError("KiCad geometry dump identity is stale")
    if (Path(dump["source_pcb_path"]).resolve() != pcb_path
            or dump["source_pcb_sha256"] != file_sha256(pcb_path)):
        raise ValueError("KiCad dump is not bound to the selected PCB")
    return {
        "kind": "kicad_volume_dump",
        "dump_path": str(dump_path),
        "dump_sha256": geometry.dump_sha256,
        "dump_content_sha256": geometry.dump_content_sha256,
        "pcb_path": str(pcb_path),
        "pcb_sha256": file_sha256(pcb_path),
        "grouping_policy": dump["grouping_policy"],
        "census": geometry.census,
        "stackup": [asdict(item) for item in stackup],
        "plating_thickness_m": geometry.plating_thickness_m,
        "geometry_tolerance_mm": geometry.geometry_tolerance_mm,
        "coordinate_grid_mm": geometry.coordinate_grid_mm,
        "simplification_area_rtol": geometry.simplification_area_rtol,
        "outer_scale": geometry.outer_scale,
        "drill_circle_points": geometry.drill_circle_points,
        "material_permittivity_overrides": dict(
            geometry.material_permittivity_overrides
        ),
    }


def load_pcb_volumes(path, stackup, **kwargs):
    path = Path(path).resolve()
    geometry = volumes_from_pcb_dump(load_pcb_volume_dump(path), stackup, **kwargs)
    return PalaceBoardGeometry(
        **{
            **geometry.__dict__,
            "dump_sha256": file_sha256(path),
        }
    )
