#!/usr/bin/env python3
"""Convert KiCad filled copper zones into FasterCap conductor surfaces."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re

import meshpy.triangle as meshpy_triangle
import shapely
from shapely import make_valid
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon
from shapely.ops import unary_union

if __package__:
    from .fastercap import ConductorSurface, extruded_triangulation_surface
    from .kicad_fastercap_schema import ZONE_DUMP_FORMAT
else:
    from fastercap import ConductorSurface, extruded_triangulation_surface
    from kicad_fastercap_schema import ZONE_DUMP_FORMAT


KICAD_GRID_MM = 1e-6
MM_TO_M = 1e-3
QUALITY_MESH_MIN_ANGLE_DEG = 20.0
QUALITY_MESH_ENGINE = "MeshPy Triangle constrained-quality"


@dataclass(frozen=True)
class StackupLayer:
    name: str
    kind: str
    thickness_mm: float
    epsilon_r: float | None
    z_top_mm: float
    z_bottom_mm: float


@dataclass(frozen=True)
class ZoneExtraction:
    surfaces: tuple[ConductorSurface, ...]
    area_mm2: dict[str, float]
    polygon_count: dict[str, int]
    triangle_count: dict[str, int]
    repaired_polygon_count: dict[str, int]
    simplification_area_error_mm2: dict[str, float]
    minimum_boundary_segment_mm: dict[str, float]
    component_count: dict[str, int]
    geometry_tolerance_mm: float
    source_mode: str = "filled_zones_only"


def _balanced_expression(text, marker):
    start = text.find(marker)
    if start < 0:
        raise ValueError(f"KiCad file has no {marker} expression")
    depth = 0
    quoted = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    raise ValueError(f"unterminated {marker} expression")


def _parse_sexpr(text):
    tokens = re.findall(r'"(?:\\.|[^"\\])*"|[^\s()]+|[()]', text)
    stack = []
    root = None
    for token in tokens:
        if token == "(":
            value = []
            if stack:
                stack[-1].append(value)
            stack.append(value)
            if root is None:
                root = value
        elif token == ")":
            if not stack:
                raise ValueError("unexpected closing parenthesis")
            stack.pop()
        else:
            if not stack:
                raise ValueError("atom outside expression")
            stack[-1].append(json.loads(token) if token.startswith('"') else token)
    if stack or root is None:
        raise ValueError("unterminated S-expression")
    return root


def _child_value(expression, key, default=None):
    for child in expression[1:]:
        if isinstance(child, list) and child and child[0] == key:
            return child[1] if len(child) > 1 else default
    return default


def parse_stackup(path):
    """Parse ordered KiCad stackup layers and assign top-down z coordinates."""
    with open(path, encoding="utf-8") as stream:
        expression = _parse_sexpr(_balanced_expression(stream.read(), "(stackup"))
    if not expression or expression[0] != "stackup":
        raise ValueError("invalid KiCad stackup expression")

    layers = []
    cursor = 0.0
    for item in expression[1:]:
        if not isinstance(item, list) or not item or item[0] != "layer":
            continue
        name = str(item[1])
        kind = str(_child_value(item, "type", ""))
        thickness = float(_child_value(item, "thickness", 0.0))
        epsilon_raw = _child_value(item, "epsilon_r")
        epsilon_r = None if epsilon_raw is None else float(epsilon_raw)
        z_top = -cursor
        cursor += thickness
        layers.append(StackupLayer(
            name=name,
            kind=kind,
            thickness_mm=thickness,
            epsilon_r=epsilon_r,
            z_top_mm=z_top,
            z_bottom_mm=-cursor,
        ))
    if not layers:
        raise ValueError("KiCad stackup contains no layers")
    return tuple(layers)


def copper_layer_bounds(stackup):
    bounds = {}
    for layer in stackup:
        if layer.kind.lower() != "copper":
            continue
        if layer.thickness_mm <= 0.0:
            raise ValueError(f"copper layer {layer.name!r} has no positive thickness")
        bounds[layer.name] = (layer.z_bottom_mm, layer.z_top_mm)
    if not bounds:
        raise ValueError("KiCad stackup contains no copper layers")
    return bounds


def _minimum_triangle_angle_deg(points):
    angles = []
    for index, origin in enumerate(points):
        first_point = points[(index + 1) % 3]
        second_point = points[(index + 2) % 3]
        first = (first_point[0] - origin[0], first_point[1] - origin[1])
        second = (second_point[0] - origin[0], second_point[1] - origin[1])
        cosine = (first[0] * second[0] + first[1] * second[1]) / (
            math.hypot(*first) * math.hypot(*second)
        )
        angles.append(math.degrees(math.acos(max(-1.0, min(1.0, cosine)))))
    return min(angles)


def _quality_triangulate_polygon(polygon):
    vertices = []
    facets = []
    holes = []
    rings = ((polygon.exterior, False),) + tuple(
        (ring, True) for ring in polygon.interiors
    )
    for ring, is_hole in rings:
        coordinates = tuple(ring.coords)[:-1]
        base = len(vertices)
        vertices.extend(coordinates)
        facets.extend(
            (base + index, base + (index + 1) % len(coordinates))
            for index in range(len(coordinates))
        )
        if is_hole:
            interior = Polygon(coordinates).representative_point()
            holes.append((interior.x, interior.y))
    information = meshpy_triangle.MeshInfo()
    information.set_points(vertices)
    information.set_facets(facets)
    information.set_holes(holes)
    mesh = meshpy_triangle.build(
        information,
        min_angle=QUALITY_MESH_MIN_ANGLE_DEG,
        quality_meshing=True,
        allow_boundary_steiner=True,
        allow_volume_steiner=True,
    )
    return tuple(
        tuple(tuple(float(value) for value in mesh.points[index]) for index in item)
        for item in mesh.elements
    )


def triangulate_polygonal(geometry, *, area_rtol=1e-9):
    """Quality-triangulate polygonal geometry without changing its boundary."""
    if geometry.is_empty:
        return ()
    polygons = (geometry.geoms if isinstance(geometry, MultiPolygon)
                else (geometry,))
    triangles = []
    for polygon in polygons:
        if not isinstance(polygon, Polygon) or not polygon.is_valid:
            raise ValueError("filled copper geometry must be valid polygons")
        selected = _quality_triangulate_polygon(polygon)
        selected_geometry = unary_union(tuple(Polygon(item) for item in selected))
        tolerance = max(1e-12, area_rtol * polygon.area)
        difference_area = selected_geometry.symmetric_difference(polygon).area
        if difference_area > tolerance:
            raise ValueError(
                "triangulation did not cover the copper polygon: "
                f"symmetric difference {difference_area:g} mm^2"
            )
        for triangle in selected:
            if len(triangle) != 3:
                raise ValueError("triangulator returned a non-triangle element")
            minimum_angle = _minimum_triangle_angle_deg(triangle)
            if minimum_angle + 1e-9 < QUALITY_MESH_MIN_ANGLE_DEG:
                raise ValueError(
                    "quality triangulation violated its minimum-angle gate: "
                    f"{minimum_angle:g} degrees"
                )
            triangles.append(triangle)
    return tuple(triangles)


def load_filled_zone_dump(path):
    with open(path, encoding="utf-8") as stream:
        dump = json.load(stream)
    if dump.get("format") != ZONE_DUMP_FORMAT:
        raise ValueError("unsupported KiCad filled-zone dump format")
    if not dump.get("groups") or not isinstance(dump.get("records"), list):
        raise ValueError("invalid KiCad filled-zone dump")
    return dump


def _polygonal_parts(geometry):
    if isinstance(geometry, Polygon):
        return (geometry,)
    if isinstance(geometry, (MultiPolygon, GeometryCollection)):
        return tuple(
            polygon
            for child in geometry.geoms
            for polygon in _polygonal_parts(child)
        )
    return ()


def _snap_to_kicad_grid(geometry):
    snapped = shapely.set_precision(geometry, KICAD_GRID_MM, mode="valid_output")
    tolerance = max(1e-10, 1e-8 * geometry.area)
    if abs(snapped.area - geometry.area) > tolerance:
        raise ValueError(
            "snapping repaired geometry to the KiCad grid changed its area: "
            f"{snapped.area:g} != {geometry.area:g} mm^2"
        )
    return snapped


def _record_geometry(record):
    polygon = Polygon(record["shell"], record.get("holes", ()))
    if polygon.is_valid:
        return _snap_to_kicad_grid(polygon), 0
    fixed = make_valid(polygon)
    parts = _polygonal_parts(fixed)
    fixed_area = sum(part.area for part in parts)
    tolerance = max(1e-12, 1e-9 * polygon.area)
    if not parts or abs(fixed_area - polygon.area) > tolerance:
        raise ValueError(
            "repairing a KiCad filled-zone contour changed its area: "
            f"{fixed_area:g} != {polygon.area:g} mm^2"
        )
    return _snap_to_kicad_grid(unary_union(parts)), 1


def _boundary_segment_lengths(geometry):
    lengths = []
    for polygon in _polygonal_parts(geometry):
        for ring in (polygon.exterior, *polygon.interiors):
            coordinates = tuple(ring.coords)
            lengths.extend(
                ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
                for (ax, ay), (bx, by) in zip(coordinates, coordinates[1:])
            )
    return tuple(lengths)


def _condition_geometry(geometry, tolerance_mm, area_rtol):
    original_area = float(geometry.area)
    conditioned = geometry.simplify(tolerance_mm, preserve_topology=True)
    conditioned = _snap_to_kicad_grid(conditioned)
    area_error = float(conditioned.area) - original_area
    allowed = max(1e-10, area_rtol * original_area)
    if abs(area_error) > allowed:
        raise ValueError(
            "geometry simplification changed filled-copper area beyond its gate: "
            f"{area_error:g} mm^2 > {allowed:g} mm^2"
        )
    lengths = _boundary_segment_lengths(conditioned)
    if not lengths or min(lengths) < tolerance_mm * 0.5:
        minimum = min(lengths) if lengths else 0.0
        raise ValueError(
            "geometry simplification left a boundary segment below half its "
            f"tolerance: {minimum:g} mm"
        )
    return conditioned, area_error, min(lengths)


def surfaces_from_filled_zone_dump(dump, stackup, *,
                                    geometry_tolerance_mm=1e-3,
                                    simplification_area_rtol=1e-4):
    """Union and triangulate a KiCad JSON contour dump under system Python."""
    geometry_tolerance_mm = float(geometry_tolerance_mm)
    simplification_area_rtol = float(simplification_area_rtol)
    if (not math.isfinite(geometry_tolerance_mm)
            or not math.isfinite(simplification_area_rtol)
            or geometry_tolerance_mm <= 0.0
            or simplification_area_rtol < 0.0):
        raise ValueError("geometry tolerances must be finite, positive, and non-negative")
    if dump.get("format") != ZONE_DUMP_FORMAT:
        raise ValueError("unsupported KiCad filled-zone dump format")
    groups = tuple(str(group) for group in dump.get("groups", ()))
    if not groups or len(set(groups)) != len(groups):
        raise ValueError("filled-zone dump groups must be non-empty and unique")
    bounds = copper_layer_bounds(stackup)
    geometries = {}
    repaired_by_group = {group: 0 for group in groups}
    for record in dump.get("records", ()):
        group = str(record.get("group", ""))
        layer = str(record.get("layer", ""))
        if group not in repaired_by_group:
            raise ValueError(f"filled-zone record has unknown group {group!r}")
        if layer not in bounds:
            continue
        geometry, repaired = _record_geometry(record)
        geometries.setdefault((group, layer), []).append(geometry)
        repaired_by_group[group] += repaired

    panels_by_group = {group: [] for group in groups}
    parts_by_group = {group: [] for group in groups}
    area_by_group = {group: 0.0 for group in groups}
    polygons_by_group = {group: 0 for group in groups}
    triangles_by_group = {group: 0 for group in groups}
    area_error_by_group = {group: 0.0 for group in groups}
    minimum_segment_by_group = {group: float("inf") for group in groups}
    for (group, layer), parts in geometries.items():
        geometry = _snap_to_kicad_grid(unary_union(parts))
        if not geometry.is_valid:
            raise ValueError("unioned KiCad filled-zone geometry is invalid")
        geometry, area_error, minimum_segment = _condition_geometry(
            geometry, geometry_tolerance_mm, simplification_area_rtol
        )
        polygon_parts = tuple(_polygonal_parts(geometry))
        z_min_mm, z_max_mm = bounds[layer]
        triangle_count = 0
        for polygon in polygon_parts:
            triangles_mm = triangulate_polygonal(polygon)
            triangles_m = tuple(
                tuple((x * MM_TO_M, y * MM_TO_M) for x, y in triangle)
                for triangle in triangles_mm
            )
            surface = extruded_triangulation_surface(
                group,
                triangles_m,
                z_min_mm * MM_TO_M,
                z_max_mm * MM_TO_M,
            )
            panels_by_group[group].extend(surface.panels)
            parts_by_group[group].append(surface.panels)
            triangle_count += len(triangles_mm)
        area_by_group[group] += float(geometry.area)
        polygons_by_group[group] += len(polygon_parts)
        triangles_by_group[group] += triangle_count
        area_error_by_group[group] += area_error
        minimum_segment_by_group[group] = min(
            minimum_segment_by_group[group], minimum_segment
        )

    missing = [group for group, panels in panels_by_group.items() if not panels]
    if missing:
        raise ValueError(f"no filled-zone copper found for groups: {', '.join(missing)}")
    return ZoneExtraction(
        surfaces=tuple(
            ConductorSurface(
                group,
                tuple(panels_by_group[group]),
                parts=tuple(parts_by_group[group]),
            )
            for group in groups
        ),
        area_mm2=area_by_group,
        polygon_count=polygons_by_group,
        triangle_count=triangles_by_group,
        repaired_polygon_count=repaired_by_group,
        simplification_area_error_mm2=area_error_by_group,
        minimum_boundary_segment_mm=minimum_segment_by_group,
        component_count={
            group: len(parts_by_group[group]) for group in groups
        },
        geometry_tolerance_mm=geometry_tolerance_mm,
    )


def load_filled_zone_surfaces(path, stackup, **kwargs):
    return surfaces_from_filled_zone_dump(
        load_filled_zone_dump(path), stackup, **kwargs
    )
