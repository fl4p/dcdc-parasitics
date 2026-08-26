#!/usr/bin/env python3
"""Conformal extruded-PLC meshes for Palace electrostatics."""
from dataclasses import asdict
import json
import math
from pathlib import Path

import meshpy
import meshpy.triangle as meshpy_triangle
import numpy as np
from shapely import set_precision, union_all
from shapely.geometry import LineString, Point, Polygon, box
from shapely.prepared import prep
from shapely.strtree import STRtree

if __package__:
    from .palace_mesh import (
        BoxBounds,
        ConductorPrism,
        DielectricBox,
        DielectricPrism,
        PalaceMeshResult,
        tetrahedral_mesh_counts,
        validate_palace_mesh_content,
    )
    from .provenance import canonical_equal, canonical_sha256, file_sha256
else:
    from palace_mesh import (
        BoxBounds,
        ConductorPrism,
        DielectricBox,
        DielectricPrism,
        PalaceMeshResult,
        tetrahedral_mesh_counts,
        validate_palace_mesh_content,
    )
    from provenance import canonical_equal, canonical_sha256, file_sha256


PLC_MESH_MANIFEST_FORMAT = "dcdc-palace-plc-mesh-v2"


def _group_items(items):
    grouped = {}
    for item in items:
        grouped.setdefault(item.name, []).append(item)
    return tuple((name, tuple(values)) for name, values in grouped.items())


def _prism_polygon(prism):
    return Polygon(prism.rings[0], prism.rings[1:])


def _dielectric_polygon(dielectric):
    if isinstance(dielectric, DielectricBox):
        bounds = dielectric.bounds
        return box(
            bounds.minimum[0], bounds.minimum[1],
            bounds.maximum[0], bounds.maximum[1],
        )
    return _prism_polygon(dielectric)


def _dielectric_z(dielectric):
    if isinstance(dielectric, DielectricBox):
        return dielectric.bounds.minimum[2], dielectric.bounds.maximum[2]
    return dielectric.z_min, dielectric.z_max


def _noding_serialization_quantum(outer_bounds, source_quantum):
    return max(
        source_quantum * 1e-5,
        8.0 * max(
            math.ulp(abs(value))
            for value in (
                *outer_bounds.minimum[:2], *outer_bounds.maximum[:2]
            )
        ),
    )


def _geometry_lines(
        outer_bounds, conductors, dielectrics, quantum,
        source_segment_max_length=None):
    outer_ring = (
        (outer_bounds.minimum[0], outer_bounds.minimum[1]),
        (outer_bounds.maximum[0], outer_bounds.minimum[1]),
        (outer_bounds.maximum[0], outer_bounds.maximum[1]),
        (outer_bounds.minimum[0], outer_bounds.maximum[1]),
        (outer_bounds.minimum[0], outer_bounds.minimum[1]),
    )
    lines = [LineString(outer_ring)]
    for item in (*conductors, *dielectrics):
        if isinstance(item, DielectricBox):
            polygon = _dielectric_polygon(item)
            rings = (tuple(polygon.exterior.coords),)
        else:
            rings = item.rings
        for ring in rings:
            lines.append(set_precision(
                LineString((*ring, ring[0])), quantum, mode="valid_output"
            ))
    noded = union_all(lines)
    serialization_quantum = _noding_serialization_quantum(
        outer_bounds, quantum
    )
    output = []
    pending = [noded]
    while pending:
        geometry = pending.pop()
        if isinstance(geometry, LineString):
            coordinates = tuple(
                tuple(
                    round(value / serialization_quantum) * serialization_quantum
                    for value in point
                )
                for point in geometry.coords
            )
            for start, end in zip(coordinates, coordinates[1:]):
                count = (
                    max(1, math.ceil(math.dist(start, end)
                                     / source_segment_max_length))
                    if source_segment_max_length is not None else 1
                )
                points = [start]
                points.extend(
                    tuple(
                        round(
                            (((count - index) * start[axis]
                              + index * end[axis]) / count)
                            / serialization_quantum
                        ) * serialization_quantum
                        for axis in range(2)
                    )
                    for index in range(1, count)
                )
                points.append(end)
                output.extend(
                    (left, right) for left, right in zip(points, points[1:])
                    if left != right
                )
        elif hasattr(geometry, "geoms"):
            pending.extend(geometry.geoms)
        else:
            raise ValueError("unsupported planar PLC boundary geometry")
    return tuple(output)


def _planar_quantum(outer_bounds, source_identity):
    quantum = max(1e-15, 1e-12 * max(outer_bounds.lengths[:2]))
    if source_identity.get("kind") == "kicad_volume_dump":
        source_grid_mm = source_identity.get("coordinate_grid_mm")
        if (isinstance(source_grid_mm, bool)
                or not isinstance(source_grid_mm, (int, float))
                or not math.isfinite(source_grid_mm) or source_grid_mm <= 0.0):
            raise ValueError("KiCad source coordinate grid is invalid")
        quantum = max(quantum, float(source_grid_mm) * 1e-3)
    return quantum


def _conductor_boundary(conductors):
    return union_all([_prism_polygon(item).boundary for item in conductors])


def _resplit_conductor_segments(segments, boundary, maximum_length, quantum):
    """Subdivide only those segments that lie on a conductor boundary.

    allow_volume_steiner is False, so Triangle may not split a segment. Without
    this the conductor polyline keeps the global sqrt(2*max_area) spacing however
    small the local area cap is, and the edge the cap exists to resolve stays
    unresolved.
    """
    near = prep(boundary.buffer(quantum * 4.0))
    output = []
    for start, end in segments:
        if not (near.intersects(Point(*start)) and near.intersects(Point(*end))):
            output.append((start, end))
            continue
        count = max(1, math.ceil(math.dist(start, end) / maximum_length))
        points = [start]
        points.extend(
            tuple((start[axis] * (count - index) + end[axis] * index) / count
                  for axis in range(2))
            for index in range(1, count)
        )
        points.append(end)
        output.extend(
            (left, right) for left, right in zip(points, points[1:])
            if left != right
        )
    return tuple(output)


def _crossing_tolerance_m(coordinate_scale_m):
    """How deep may a source segment enter a cell before it is a real crossing?

    Shapely's `covers` is exact, so a segment lying *on* a triangle edge reads
    as a crossing whenever the two representations differ in the last bit. On
    the canary at refinement 3 that produced 2 flagged cells out of 17089, the
    worst penetrating 3.9e-17 m -- about one ULP of a 0.167 m coordinate, and
    nine orders of magnitude below the 5e-8 m quantum the geometry is snapped
    to. Below this bound "crossing" is a statement about IEEE754, not the mesh.

    The bound is ULP-scaled and *not* the geometry quantum on purpose. A
    tolerance at the quantum would be a mute button: it would wave through a
    conductor boundary genuinely cut by tens of nanometres, which is a real
    material-assignment error rather than a rounding artefact.
    """
    return 8.0 * math.ulp(coordinate_scale_m)


def _coverage_grid_m(coordinate_scale_m, quantum_m):
    """The grid used to re-decide "does this segment lie on this edge", or None.

    A residue length cannot answer that question by itself. A segment lying
    along an edge that is bent off it by one ULP shares only a measure-zero set
    with it, so `difference` returns the whole segment -- indistinguishable, by
    length, from a segment that was dropped outright. Snapping both sides to a
    common grid makes them exactly collinear again. Measured on the canary after
    one uniform subdivision: 689 segments report 100% uncovered while straying
    at most 2.794e-17 m from the edges covering them, against 1.9e-3 m for
    segments Triangle genuinely dropped.

    The grid is relative to the coordinate magnitude because that is what sets
    the rounding being absorbed, and it is rounded down to a power of ten. Both
    of those are load-bearing and were measured, not assumed:

    - It cannot sit near the ULP. On a 2e-3 m fixture a segment whose raw
      residue is exactly 0.0 comes back 100% uncovered when snapped to
      1.1e-16, and correct from 1e-15 up.
    - It has to be a power of ten. GEOS snaps by scaling by 1/gridSize, so a
      grid that is not exactly representable rounds inconsistently and pushes
      collinear points off each other. On the canary the same comparison that
      leaves 0 segments uncovered at 1e-13 leaves 160 at 2.05e-13 and 141 to
      193 at every power of two from 2^-46 to 2^-36. The source coordinates are
      themselves a decimal grid, which is the company this keeps.

    Snapping is the fallback, never the primary test, for the first reason.

    Returns None when no safe grid exists -- when it would approach the quantum
    the source geometry itself is snapped to, and would start absorbing real
    geometry rather than rounding. The caller then keeps the unsnapped verdict,
    which fails closed.
    """
    grid = 10.0 ** math.floor(math.log10(coordinate_scale_m * 1e-12))
    # Two decades of clearance, not a hair's breadth. The grid moves in factors
    # of ten, so a threshold set just below it would be decided by where the
    # quantum happens to fall between two decades rather than by whether there
    # is real separation -- the plated-via fixture lands within 0.03% of a
    # quantum/4 threshold, which is luck, not headroom.
    if grid > quantum_m / 100.0:
        return None
    return grid


def _boundary_crossings(cells, source_lines, source_tree, tolerance_m, grid_m):
    """Cells whose interior a source boundary passes through.

    Two-stage on purpose. The exact test is cheap and clears the overwhelming
    majority of cells, but it cannot be trusted on its own: a segment lying
    *along* a cell edge shares only a measure-zero set with it once the two
    representations differ in the last bit, so `covers` says no and the
    "depth" comes back as the entire overlap. After one uniform subdivision
    that mislabelled 1356 of 24824 cells, the worst reporting a 1.765e-3 m
    penetration into a cell about 1e-3 m across -- an impossible depth, which
    is what gives the artefact away.

    Anything the exact test flags is therefore re-tested against both geometries
    snapped to a grid, which makes an edge-lying segment exactly collinear
    again. Real crossings survive snapping; the grid is four orders below the
    quantum the geometry itself is snapped to.

    Returns every crossing rather than raising at the first. One triangle's
    coordinates cannot distinguish "the mesher ignored a constraint" from "one
    sliver grazes a boundary", and that is the first question asked; the count
    and the worst depth answer it.
    """
    crossings = []
    for index, polygon in enumerate(cells):
        snapped = None
        for line_index in source_tree.query(polygon):
            line = source_lines[int(line_index)]
            intersection = polygon.intersection(line)
            if intersection.is_empty or polygon.boundary.covers(intersection):
                continue
            if grid_m is not None:
                if snapped is None:
                    snapped = set_precision(polygon, grid_m)
                exact = set_precision(line, grid_m)
                intersection = snapped.intersection(exact)
                if (intersection.is_empty
                        or snapped.boundary.covers(intersection)):
                    continue
                depth = intersection.difference(snapped.boundary).length
            else:
                depth = intersection.difference(polygon.boundary).length
            if depth <= tolerance_m:
                continue
            crossings.append((index, depth, list(polygon.exterior.coords)))
    return crossings


def _subdivide_uniformly(points, triangles, refinements):
    """Split every triangle into four by its edge midpoints, `refinements` times.

    This replaces Triangle's -r mode as the nesting mechanism, because -r cannot
    be trusted to keep the PLC segments however it is invoked. Declaring them
    with `p` (which `meshpy.triangle.refine` never does -- it gates the flag on
    `faces`, the output edge array, while the segments live in `facets`) fixes
    the shallow case and not the deep one. Measured, counting source segments no
    longer covered by any triangle edge:

        seed 8e-6, rounds 0-4      0 uncovered        round 5    2 uncovered
        seed 5e-8, rounds 0-1      0 uncovered        round 2   23 uncovered
                                                      round 3   77 uncovered
                                                      round 4  108 uncovered

    The lost segments are dropped from Triangle's own segment list, and it is
    not the input's fault: of 2532 source segments exactly one pair meets
    anywhere other than a shared endpoint, and that pair is a duplicate, so the
    PSLG is valid. Neither dropping `j` nor adding `Y` changes the count, and
    quality meshing makes it far worse (46 uncovered, 100%).

    Uniform subdivision has none of that surface because no mesher is involved:

    - Every parent vertex is a child vertex, so the rungs are exactly nested and
      Rayleigh-Ritz monotonicity is owed rather than hoped for.
    - Every parent edge becomes two collinear halves, so a source segment that
      was an edge stays covered by edges, by construction rather than by luck.
    - The four children are similar to the parent, so shape quality is exactly
      preserved and a graded seed keeps its grading.
    - Element size halves exactly each rung, which is a cleaner ladder than
      halving an area (a sqrt(2) step in length).

    The price is 4x the triangles per rung instead of 2x. For a convergence
    ladder that is the right trade: an inexactly nested ladder measures
    re-meshing noise, and on this model that noise moved 45 of 171 matrix
    entries by more than the whole acceptance band.

    Midpoints are keyed by the sorted index pair, so the two triangles sharing
    an edge get the identical vertex and the result is conforming -- no hanging
    nodes, nothing to reconcile.
    """
    points = list(points)
    index = {point: position for position, point in enumerate(points)}
    for _ in range(refinements):
        midpoints = {}

        def midpoint(left, right):
            key = (left, right) if left < right else (right, left)
            found = midpoints.get(key)
            if found is not None:
                return found
            start, end = points[key[0]], points[key[1]]
            candidate = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
            found = index.get(candidate)
            if found is None:
                found = len(points)
                index[candidate] = found
                points.append(candidate)
            midpoints[key] = found
            return found

        divided = []
        for first, second, third in triangles:
            left = midpoint(first, second)
            right = midpoint(second, third)
            base = midpoint(third, first)
            divided.append((first, left, base))
            divided.append((left, second, right))
            divided.append((base, right, third))
            divided.append((left, right, base))
        triangles = divided
    return tuple(points), tuple(triangles)


def _nested_refinement(points, triangles, refinements):
    """Subdivide uniformly and verify the result really is nested.

    Calling build() again at a finer constraint returns an unrelated
    triangulation. That costs a convergence ladder both things it depends on:
    Rayleigh-Ritz monotonicity is owed only to nested spaces, and the re-meshing
    perturbation adds on every difference instead of cancelling. Measured on the
    canary, regenerating at the same resolution moved 45 of 171 matrix entries
    by more than the whole acceptance band, the worst by 63.8%, while the trace
    moved 0.238% and hid it.

    Nesting is checked here rather than trusted, while the parent is still in
    hand. It is guaranteed by construction for uniform subdivision, so this
    guard is aimed at a bug in that construction, not at the mesher -- and it is
    deliberately not the only check: `_validate_plc_mesh_topology` still has to
    find every source segment covered downstream, because a vertex check passes
    cleanly on a refinement that kept every vertex and dropped every segment,
    which is exactly what Triangle's -r mode did.
    """
    parent = set(points)
    expected = len(triangles) * 4 ** refinements
    points, triangles = _subdivide_uniformly(points, triangles, refinements)
    lost = parent - set(points)
    if lost:
        raise ValueError(
            f"Palace PLC nested refinement lost {len(lost)} of {len(parent)} "
            f"parent vertices: the rung is not nested in its parent, so a "
            f"ladder over it would carry re-meshing noise it is meant to have "
            f"removed")
    # Containment alone would be satisfied by doing nothing at all, and a rung
    # that silently did not refine still records its nesting_refinements and
    # would enter the ladder as a duplicate of its parent. The manifest cannot
    # catch that either: max_planar_area_m2 stays at the seed value on every
    # rung, so the recorded cap is trivially met however little was done.
    if len(triangles) != expected:
        raise ValueError(
            f"Palace PLC nested refinement produced {len(triangles)} triangles "
            f"where {refinements} uniform splits of {len(parent)} vertices owe "
            f"exactly {expected}: the rung did not refine as recorded")
    return points, triangles


def _nesting_parameters_valid(parameters):
    """Are the nesting parameters absent together, or present and consistent?

    They must move as a pair. A manifest carrying `nesting_refinements` without
    `plc_segments_intact` would let a reader assume segments are intact on a
    refined mesh, and one claiming intact segments at a positive refinement
    count would assert something Triangle's -r mode does not provide.
    """
    refinements = parameters.get("nesting_refinements")
    intact = parameters.get("plc_segments_intact")
    if "nesting_refinements" not in parameters:
        # Written before nesting existed; the pair is absent and that is a
        # complete, unambiguous statement of an unnested mesh.
        return "plc_segments_intact" not in parameters
    if "plc_segments_intact" not in parameters:
        return False
    if refinements is not None and (
            isinstance(refinements, bool) or not isinstance(refinements, int)
            or refinements < 0):
        return False
    return intact is (not refinements)


def _planar_mesh(outer_bounds, conductors, dielectrics, max_area_m2, quantum,
                 conductor_edge_band_m=None,
                 conductor_edge_max_planar_area_m2=None,
                 nesting_refinements=None):
    source_segment_max_length = (
        math.sqrt(2.0 * max_area_m2) if max_area_m2 is not None else None
    )
    segments = _geometry_lines(
        outer_bounds, conductors, dielectrics, quantum,
        source_segment_max_length,
    )
    refinement_func = None
    zone = None
    if conductor_edge_band_m is not None:
        boundary = _conductor_boundary(conductors)
        segments = _resplit_conductor_segments(
            segments, boundary,
            math.sqrt(2.0 * conductor_edge_max_planar_area_m2), quantum,
        )
        # Triangle's -u callback: the field singularity at a conductor edge has
        # the copper thickness as its length scale, so the cap is applied by
        # distance from the boundary rather than uniformly.
        zone = prep(boundary.buffer(conductor_edge_band_m))

        def refinement_func(vertices, area):
            if area <= conductor_edge_max_planar_area_m2:
                return False
            return zone.intersects(Point(
                sum(vertex[0] for vertex in vertices) / 3.0,
                sum(vertex[1] for vertex in vertices) / 3.0,
            ))

    point_ids = {}
    points = []

    def point_id(point):
        key = tuple(float(value) for value in point)
        if key not in point_ids:
            point_ids[key] = len(points)
            points.append(key)
        return point_ids[key]

    facets = []
    seen_facets = set()
    for start, end in segments:
        left = point_id(start)
        right = point_id(end)
        facet = tuple(sorted((left, right)))
        if left != right and facet not in seen_facets:
            seen_facets.add(facet)
            facets.append(facet)
    information = meshpy_triangle.MeshInfo()
    information.set_points(points)
    information.set_facets(facets)
    mesh = meshpy_triangle.build(
        information,
        quality_meshing=False,
        max_volume=max_area_m2,
        allow_boundary_steiner=False,
        # MeshPy maps this to Triangle's YY option, preserving internal PLC
        # segments while still permitting interior area-refinement points.
        allow_volume_steiner=False,
        refinement_func=refinement_func,
    )
    canonical_ids = {}
    mesh_points = []
    remap = {}
    for index, point in enumerate(mesh.points):
        key = tuple(float(value) for value in point)
        if key not in canonical_ids:
            canonical_ids[key] = len(mesh_points)
            mesh_points.append(key)
        remap[index] = canonical_ids[key]
    triangles = []
    seen_triangles = set()
    for item in mesh.elements:
        triangle = tuple(remap[int(value)] for value in item)
        key = tuple(sorted(triangle))
        if len(set(triangle)) == 3 and key not in seen_triangles:
            seen_triangles.add(key)
            triangles.append(triangle)
    mesh_points = tuple(mesh_points)
    triangles = tuple(triangles)
    if not mesh_points or not triangles:
        raise RuntimeError("MeshPy produced an empty planar PLC")
    # Subdivision runs on the canonicalised triangulation, after duplicate
    # points have been merged, so a midpoint cannot land on a coordinate that
    # exists under a second index and silently unweld two triangles.
    if nesting_refinements:
        mesh_points, triangles = _nested_refinement(
            mesh_points, triangles, nesting_refinements)
    if max_area_m2 is not None:
        maximum_area = max(
            abs(
                (mesh_points[item[1]][0] - mesh_points[item[0]][0])
                * (mesh_points[item[2]][1] - mesh_points[item[0]][1])
                - (mesh_points[item[1]][1] - mesh_points[item[0]][1])
                * (mesh_points[item[2]][0] - mesh_points[item[0]][0])
            ) / 2.0
            for item in triangles
        )
        serialization_bound = 8.0 * max(
            math.ulp(maximum_area), math.ulp(max_area_m2)
        )
        if maximum_area > max_area_m2 + serialization_bound:
            raise ValueError("MeshPy exceeded the maximum planar triangle area")
    return mesh_points, triangles


def _cell_region(point, z, conductors, conductor_polygons,
                 dielectrics, dielectric_polygons, material_attributes):
    location = Point(point)
    conductor_matches = {
        item.name for item, polygon in zip(conductors, conductor_polygons)
        if item.z_min < z < item.z_max and polygon.contains(location)
    }
    if len(conductor_matches) > 1:
        raise ValueError("different conductor groups overlap in the PLC")
    if conductor_matches:
        return "conductor", next(iter(conductor_matches))
    dielectric_matches = {
        item.name for item, polygon in zip(dielectrics, dielectric_polygons)
        if _dielectric_z(item)[0] < z < _dielectric_z(item)[1]
        and polygon.contains(location)
    }
    if len(dielectric_matches) > 1:
        raise ValueError("different dielectric groups overlap in the PLC")
    if dielectric_matches:
        name = next(iter(dielectric_matches))
        return "material", material_attributes[name]
    return "material", 1


def _tetrahedron_determinants(points):
    first = points[..., 1, :] - points[..., 0, :]
    second = points[..., 2, :] - points[..., 0, :]
    third = points[..., 3, :] - points[..., 0, :]
    return (
        first[..., 0] * (
            second[..., 1] * third[..., 2] - second[..., 2] * third[..., 1]
        )
        - first[..., 1] * (
            second[..., 0] * third[..., 2] - second[..., 2] * third[..., 0]
        )
        + first[..., 2] * (
            second[..., 0] * third[..., 1] - second[..., 1] * third[..., 0]
        )
    )


def _oriented_tetrahedron(nodes, coordinates):
    points = np.asarray([coordinates[index] for index in nodes], dtype=float)
    determinant = float(_tetrahedron_determinants(points))
    if not math.isfinite(determinant) or abs(determinant) <= 1e-30:
        raise RuntimeError(
            "PLC extrusion produced a degenerate tetrahedron: "
            f"nodes={nodes}, determinant={determinant:.17g}, points={points.tolist()}"
        )
    if determinant < 0.0:
        return (nodes[0], nodes[2], nodes[1], nodes[3]), -determinant
    return nodes, determinant


def _coalesce_levels(values, span):
    tolerance = max(1e-15, 1e-10 * span)
    groups = []
    for value in sorted(values):
        if groups and value - groups[-1][-1] <= tolerance:
            groups[-1].append(value)
        else:
            groups.append([value])
    return tuple(sum(group) / len(group) for group in groups)


def _subdivision_count(gap, maximum_step, nested):
    """How many equal pieces a gap is cut into.

    `ceil(gap / step)` is the fewest pieces that satisfy the step, and it is
    what a single mesh wants. It is the wrong answer for a *ladder*: halving the
    step takes a 1.51 mm core from 16 pieces to 31, and equal division at 16 and
    at 31 share almost no interior level, so the coarse level set is not a
    subset of the fine one. Measured between two real rungs, 15 of 25 coarse
    z-levels were absent from the fine mesh.

    Rounding up to a power of two instead makes halving the step double the
    count exactly, and equal division into 2n pieces contains every division
    point of n pieces. The coarse levels then survive bit-for-bit -- index 2k of
    2n and index k of n are the same IEEE754 quotient, because scaling a
    correctly-rounded division by an exact power of two changes nothing.

    The cost is up to 2x more levels than the step strictly requires, which is
    the price of a ladder whose rungs are nested.
    """
    count = max(1, math.ceil(gap / maximum_step))
    if not nested:
        return count
    return 2 ** (count - 1).bit_length()


def _refine_levels(levels, maximum_step, band=None, nested=False):
    if maximum_step is None:
        return levels
    # The band edges are supplied as round numbers while the levels are derived
    # from the stackup, so the two agree only to within floating-point error:
    # the canary's -1.6 mm board bottom lands at -0.0015999999999999999, which
    # is 2.2e-19 m *above* a band edge of -0.0016. Comparing exactly there
    # un-skips the 44 mm air gap below the board and tiles it at the board's own
    # step. That fails open -- the mesh is 20x larger than intended and nothing
    # reports it -- so the comparison is made at the same relative tolerance
    # _coalesce_levels uses to decide that two levels are the same level.
    tolerance = max(1e-15, 1e-10 * abs(levels[-1] - levels[0]))
    refined = [levels[0]]
    for low, high in zip(levels, levels[1:]):
        # A band confines subdivision to gaps overlapping it, so refining the
        # board stackup does not also tile the surrounding air.
        if band is not None and (high <= band[0] + tolerance
                                 or low >= band[1] - tolerance):
            refined.append(high)
            continue
        count = _subdivision_count(high - low, maximum_step, nested)
        refined.extend(low + (high - low) * index / count
                       for index in range(1, count))
        # Interpolating the last point rather than reusing `high` lands it up to
        # an ulp past the gap it closes, which puts a spurious level just inside
        # the neighbouring gap and breaks the invariant that every input level
        # survives refinement exactly.
        refined.append(high)
    return tuple(refined)


def _validate_vertical_refinement_band(band):
    if band is None:
        return None
    if isinstance(band, (str, bytes, dict)):
        raise ValueError("vertical refinement band must be null or a 2-sequence")
    try:
        values = tuple(band)
    except TypeError:
        raise ValueError(
            "vertical refinement band must be null or a 2-sequence") from None
    if len(values) != 2 or any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) for value in values):
        raise ValueError("vertical refinement band must be null or a 2-sequence")
    if not values[0] < values[1]:
        raise ValueError("vertical refinement band must be increasing")
    return (float(values[0]), float(values[1]))


# Triangle may not split a segment (allow_volume_steiner is False), so a few
# elements wedged against an unsplittable segment cannot reach the in-band area
# cap however hard it is asked. Measured on the canary: 1 of 27,523 in-band
# triangles, at 2.39x. These bound that, while still failing decisively on a
# mesh whose recorded refinement was never actually applied -- there essentially
# every in-band triangle is over the cap.
MAX_EDGE_AREA_OVERSHOOT = 8.0
MAX_EDGE_AREA_VIOLATION_FRACTION = 0.01


def _validate_conductor_edge_refinement(band_m, max_area_m2, base_area_m2):
    """Fail closed on the graded lateral refinement spec.

    Both halves or neither: a band with no cap refines nothing, and a cap with
    no band is a global cap wearing a local name. The cap must also be at least
    as fine as the base area, or "refinement" would coarsen the very elements it
    names.
    """
    if band_m is None and max_area_m2 is None:
        return None, None
    if band_m is None or max_area_m2 is None:
        raise ValueError(
            "conductor edge refinement needs both a band and a maximum area")
    for value, label in ((band_m, "conductor edge band"),
                         (max_area_m2, "conductor edge maximum planar area")):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0.0):
            raise ValueError(f"{label} must be finite and positive")
    if base_area_m2 is None:
        raise ValueError(
            "conductor edge refinement needs a maximum planar area to refine")
    if max_area_m2 > base_area_m2:
        raise ValueError(
            "conductor edge maximum planar area must not exceed the global one")
    return float(band_m), float(max_area_m2)


def _tetrahedralize(points_2d, triangles, z_levels, region_for_cell):
    count = len(points_2d)
    coordinates = tuple(
        (point[0], point[1], z)
        for z in z_levels for point in points_2d
    )
    tetrahedra = []
    for layer, (z_bottom, z_top) in enumerate(zip(z_levels, z_levels[1:])):
        z_mid = 0.5 * (z_bottom + z_top)
        for triangle in triangles:
            ordered = tuple(sorted(triangle))
            centroid = tuple(
                sum(points_2d[index][axis] for index in triangle) / 3.0
                for axis in range(2)
            )
            region = region_for_cell(centroid, z_mid)
            bottom = tuple(layer * count + index for index in ordered)
            top = tuple((layer + 1) * count + index for index in ordered)
            candidates = (
                (bottom[0], bottom[1], bottom[2], top[2]),
                (bottom[0], bottom[1], top[1], top[2]),
                (bottom[0], top[0], top[1], top[2]),
            )
            for nodes in candidates:
                oriented, determinant = _oriented_tetrahedron(nodes, coordinates)
                tetrahedra.append((oriented, region, determinant))
    return coordinates, tuple(tetrahedra)


def _tetrahedron_faces(nodes):
    a, b, c, d = nodes
    return ((b, c, d), (a, d, c), (a, b, d), (a, c, b))


def _outer_face(nodes, coordinates, outer_bounds):
    tolerance = max(1e-14, 1e-10 * max(outer_bounds.lengths))
    for axis in range(3):
        values = [coordinates[node][axis] for node in nodes]
        if (all(abs(value - outer_bounds.minimum[axis]) <= tolerance
                for value in values)
                or all(abs(value - outer_bounds.maximum[axis]) <= tolerance
                       for value in values)):
            return True
    return False


def _domain_elements(coordinates, tetrahedra, terminal_attributes, outer_bounds):
    faces = {}
    for nodes, region, _ in tetrahedra:
        for face in _tetrahedron_faces(nodes):
            faces.setdefault(tuple(sorted(face)), []).append(region)
    boundary_triangles = []
    for nodes, regions in faces.items():
        if len(regions) > 2:
            raise RuntimeError("non-manifold PLC face")
        if len(regions) == 1:
            if regions[0][0] == "conductor":
                raise RuntimeError("open conductor boundary in the PLC")
            if not _outer_face(nodes, coordinates, outer_bounds):
                raise RuntimeError("open material boundary inside the PLC")
            boundary_triangles.append((nodes, 9999))
            continue
        left, right = regions
        kinds = {left[0], right[0]}
        if kinds == {"material"}:
            continue
        if kinds == {"conductor"}:
            if left != right:
                raise ValueError("different conductor groups touch in the PLC")
            continue
        conductor = left if left[0] == "conductor" else right
        boundary_triangles.append((nodes, terminal_attributes[conductor[1]]))
    domain_records = tuple(
        (nodes, region[1], determinant)
        for nodes, region, determinant in tetrahedra
        if region[0] == "material"
    )
    domain_tetrahedra = tuple(
        (nodes, attribute) for nodes, attribute, _ in domain_records
    )
    if not domain_tetrahedra or not boundary_triangles:
        raise RuntimeError("PLC produced no Palace domain or boundary elements")
    seen_markers = {marker for _, marker in boundary_triangles}
    expected_markers = {9999, *terminal_attributes.values()}
    if seen_markers != expected_markers:
        raise RuntimeError("PLC boundary markers are incomplete")
    minimum_determinant = min(item[2] for item in domain_records)
    return tuple(boundary_triangles), domain_tetrahedra, minimum_determinant


def _write_msh(path, coordinates, boundary_triangles, tetrahedra,
               terminal_attributes, material_attributes):
    used = sorted({
        node for nodes, _ in (*boundary_triangles, *tetrahedra) for node in nodes
    })
    node_ids = {node: index + 1 for index, node in enumerate(used)}
    physical_names = [
        *(f'2 {attribute} "{name}"' for name, attribute in terminal_attributes),
        '2 9999 "electrostatic_infinity_boundary"',
        *(f'3 {attribute} "{name}"'
          for name, attribute, _ in material_attributes),
    ]
    sorted_triangles = sorted(
        boundary_triangles, key=lambda item: (item[1], tuple(sorted(item[0])))
    )
    lines = [
        "$MeshFormat", "2.2 0 8", "$EndMeshFormat",
        "$PhysicalNames", str(len(physical_names)), *physical_names,
        "$EndPhysicalNames", "$Nodes", str(len(used)),
    ]
    lines.extend(
        f"{node_ids[node]} " + " ".join(f"{value:.17g}" for value in coordinates[node])
        for node in used
    )
    lines.extend(("$EndNodes", "$Elements", str(len(sorted_triangles) + len(tetrahedra))))
    element_id = 1
    for nodes, attribute in sorted_triangles:
        lines.append(
            f"{element_id} 2 2 {attribute} {attribute} "
            + " ".join(str(node_ids[node]) for node in nodes)
        )
        element_id += 1
    for nodes, attribute in tetrahedra:
        lines.append(
            f"{element_id} 4 2 {attribute} {attribute} "
            + " ".join(str(node_ids[node]) for node in nodes)
        )
        element_id += 1
    lines.extend(("$EndElements", ""))
    path.write_text("\n".join(lines))
    return len(used), len(tetrahedra)


def _meshpy_identity():
    package_path = Path(meshpy.__file__).resolve()
    internals = sorted(package_path.parent.glob("_internals*.so"))
    if len(internals) != 1:
        raise RuntimeError("MeshPy native extension identity is missing or ambiguous")
    return {
        "backend": "meshpy-triangle-extrusion",
        "version": meshpy.__version__,
        "python_package": str(package_path),
        "python_package_sha256": file_sha256(package_path),
        "native_extension": str(internals[0].resolve()),
        "native_extension_sha256": file_sha256(internals[0]),
    }


def _validate_source_identity(source):
    if not isinstance(source, dict) or not isinstance(source.get("kind"), str):
        raise ValueError("Palace PLC source identity is invalid")
    if source["kind"] == "direct_geometry":
        if set(source) != {"kind"}:
            raise ValueError("direct PLC source identity has unknown fields")
        return None
    required = {
        "census", "coordinate_grid_mm", "drill_circle_points",
        "dump_content_sha256", "dump_path", "dump_sha256",
        "geometry_tolerance_mm", "grouping_policy", "kind",
        "material_permittivity_overrides", "outer_scale", "pcb_path",
        "pcb_sha256", "plating_thickness_m", "simplification_area_rtol",
        "stackup",
    }
    if source["kind"] != "kicad_volume_dump" or set(source) != required:
        raise ValueError("unsupported Palace PLC source identity")
    if (not isinstance(source["dump_path"], str)
            or not isinstance(source["pcb_path"], str)
            or file_sha256(source["dump_path"]) != source["dump_sha256"]
            or file_sha256(source["pcb_path"]) != source["pcb_sha256"]):
        raise ValueError("Palace PLC source file identity mismatch")
    try:
        dump = json.loads(Path(source["dump_path"]).read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid Palace PLC source dump: {error}") from error
    if (canonical_sha256(dump) != source["dump_content_sha256"]
            or dump.get("grouping_policy") != source["grouping_policy"]
            or not canonical_equal(dump.get("census"), source["census"])
            or Path(str(dump.get("source_pcb_path"))).resolve()
            != Path(source["pcb_path"]).resolve()
            or dump.get("source_pcb_sha256") != source["pcb_sha256"]):
        raise ValueError("Palace PLC source dump semantics mismatch")
    numeric_names = (
        "coordinate_grid_mm", "geometry_tolerance_mm", "outer_scale",
        "plating_thickness_m", "simplification_area_rtol",
    )
    if (any(isinstance(source[name], bool)
            or not isinstance(source[name], (int, float))
            or not math.isfinite(source[name]) or source[name] <= 0.0
            for name in numeric_names)
            or type(source["drill_circle_points"]) is not int
            or source["drill_circle_points"] < 16
            or source["grouping_policy"] not in (
                "explicit", "all_nets_and_isolated_items")
            or not isinstance(source["stackup"], list) or not source["stackup"]
            or not isinstance(source["material_permittivity_overrides"], dict)
            or any(not isinstance(name, str) or not name
                   or isinstance(value, bool) or not isinstance(value, (int, float))
                   or not math.isfinite(value) or value <= 0.0
                   for name, value in source[
                       "material_permittivity_overrides"].items())):
        raise ValueError("Palace PLC source controls are invalid")
    if __package__:
        from .kicad_fastercap import StackupLayer, parse_stackup
        from .kicad_palace import load_pcb_volumes
    else:
        from kicad_fastercap import StackupLayer, parse_stackup
        from kicad_palace import load_pcb_volumes
    try:
        stackup = tuple(StackupLayer(**item) for item in source["stackup"])
        if not canonical_equal(
                [asdict(item) for item in parse_stackup(source["pcb_path"])],
                source["stackup"]):
            raise ValueError("stored stackup differs from the bound PCB")
        geometry = load_pcb_volumes(
            source["dump_path"],
            stackup,
            plating_thickness_m=source["plating_thickness_m"],
            material_permittivity=source["material_permittivity_overrides"],
            geometry_tolerance_mm=source["geometry_tolerance_mm"],
            simplification_area_rtol=source["simplification_area_rtol"],
            outer_scale=source["outer_scale"],
            drill_circle_points=source["drill_circle_points"],
        )
        if not canonical_equal(
                source["coordinate_grid_mm"], geometry.coordinate_grid_mm):
            raise ValueError("coordinate grid differs from reconstructed geometry")
        return geometry
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Palace PLC source reconstruction failed: {error}") from error


def generate_palace_plc_mesh(path, *, outer_bounds, conductors, dielectrics=(),
                             outer_permittivity=1.0, max_planar_area_m2=None,
                             max_vertical_step_m=None,
                             vertical_refinement_band_m=None,
                             conductor_edge_band_m=None,
                             conductor_edge_max_planar_area_m2=None,
                             nesting_refinements=None,
                             source_identity=None):
    path = Path(path).resolve()
    outer_bounds = BoxBounds(outer_bounds.minimum, outer_bounds.maximum)
    conductors = tuple(conductors)
    dielectrics = tuple(dielectrics)
    if not conductors:
        raise ValueError("at least one conductor is required")
    if (isinstance(outer_permittivity, bool)
            or not isinstance(outer_permittivity, (int, float))
            or not math.isfinite(outer_permittivity) or outer_permittivity <= 0.0):
        raise ValueError("outer permittivity must be finite and positive")
    for value, label in (
            (max_planar_area_m2, "maximum planar area"),
            (max_vertical_step_m, "maximum vertical step")):
        if (value is not None
                and (isinstance(value, bool) or not isinstance(value, (int, float))
                     or not math.isfinite(value) or value <= 0.0)):
            raise ValueError(f"{label} must be null or finite and positive")
    # None and 0 are deliberately different. None means "not a ladder rung":
    # z gaps are cut into the fewest pieces that satisfy the step, which is
    # what a standalone mesh wants and what every existing mesh recorded. 0
    # means "rung zero of a nested ladder": no planar refinement yet, but the
    # z levels already bisect, so rung 0 and rung 1 share a level set. Making
    # 0 mean None would leave the coarsest rung unnested in z against every
    # rung above it -- exactly the 15-of-25 lost levels this is meant to fix.
    if nesting_refinements is not None and (
            isinstance(nesting_refinements, bool)
            or not isinstance(nesting_refinements, int)
            or nesting_refinements < 0):
        raise ValueError(
            "nesting refinements must be null or a non-negative integer")
    if nesting_refinements and max_planar_area_m2 is None:
        # Refinement halves an area target; without one there is nothing to
        # halve, and silently meshing unrefined would make a ladder rung
        # identical to its parent while claiming to be finer.
        raise ValueError(
            "nesting refinements require a maximum planar area")
    vertical_refinement_band_m = _validate_vertical_refinement_band(
        vertical_refinement_band_m)
    conductor_edge_band_m, conductor_edge_max_planar_area_m2 = (
        _validate_conductor_edge_refinement(
            conductor_edge_band_m, conductor_edge_max_planar_area_m2,
            max_planar_area_m2))
    source_identity = json.loads(json.dumps(
        source_identity or {"kind": "direct_geometry"}, allow_nan=False
    ))
    conductor_groups = _group_items(conductors)
    dielectric_groups = _group_items(dielectrics)
    for _, values in dielectric_groups:
        if len({item.relative_permittivity for item in values}) != 1:
            raise ValueError("same-name dielectric volumes must share permittivity")
    terminal_attribute_map = {
        name: 101 + index for index, (name, _) in enumerate(conductor_groups)
    }
    material_attribute_map = {
        name: 2 + index for index, (name, _) in enumerate(dielectric_groups)
    }
    material_attributes = [
        ("outer", 1, float(outer_permittivity)),
        *((name, material_attribute_map[name], values[0].relative_permittivity)
          for name, values in dielectric_groups),
    ]
    z_levels = _refine_levels(_coalesce_levels({
        *outer_bounds.minimum[2:3], *outer_bounds.maximum[2:3],
        *(value for item in conductors for value in (item.z_min, item.z_max)),
        *(value for item in dielectrics for value in _dielectric_z(item)),
    }, outer_bounds.lengths[2]), max_vertical_step_m,
        vertical_refinement_band_m,
        nested=nesting_refinements is not None)
    planar_quantum = _planar_quantum(outer_bounds, source_identity)
    points_2d, triangles = _planar_mesh(
        outer_bounds, conductors, dielectrics, max_planar_area_m2, planar_quantum,
        conductor_edge_band_m, conductor_edge_max_planar_area_m2,
        nesting_refinements,
    )
    conductor_polygons = tuple(_prism_polygon(item) for item in conductors)
    dielectric_polygons = tuple(_dielectric_polygon(item) for item in dielectrics)
    coordinates, all_tetrahedra = _tetrahedralize(
        points_2d, triangles, z_levels,
        lambda point, z: _cell_region(
            point, z, conductors, conductor_polygons,
            dielectrics, dielectric_polygons, material_attribute_map,
        ),
    )
    boundary_triangles, tetrahedra, minimum_determinant = _domain_elements(
        coordinates, all_tetrahedra, terminal_attribute_map, outer_bounds
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    terminal_attributes = tuple(terminal_attribute_map.items())
    written_nodes, written_tetrahedra = _write_msh(
        path, coordinates, boundary_triangles, tetrahedra,
        terminal_attributes, material_attributes,
    )
    node_count, edge_count, face_count, tetrahedron_count = (
        tetrahedral_mesh_counts(path)
    )
    if (node_count != written_nodes
            or tetrahedron_count != written_tetrahedra):
        raise ValueError("emitted Palace PLC topology count mismatch")
    provenance = {
        "outer_bounds": asdict(outer_bounds),
        "outer_permittivity": float(outer_permittivity),
        "conductors": [asdict(item) for item in conductors],
        "dielectrics": [asdict(item) for item in dielectrics],
        "terminal_attributes": terminal_attributes,
        "material_attributes": material_attributes,
        "ground_attribute": 9999,
        "reference_semantics": {
            "kind": "finite_outer_dirichlet_approximation",
            "convergence_ladder_required": True,
            "not_a_circuit_node": True,
        },
        "mesh_parameters": {
            "backend": "meshpy-triangle-extrusion",
            "allow_boundary_steiner": False,
            "allow_volume_steiner": False,
            "max_planar_area_m2": max_planar_area_m2,
            "max_vertical_step_m": max_vertical_step_m,
            "conductor_edge_band_m": conductor_edge_band_m,
            "conductor_edge_max_planar_area_m2": (
                conductor_edge_max_planar_area_m2),
            "nesting_refinements": nesting_refinements,
            # allow_volume_steiner above is the build() flag, and it stays
            # true of the coarsest triangulation. Triangle's refine mode
            # takes no such flag and does split PLC segments, so a reader
            # who needs intact segments must be told that separately rather
            # than inferring it from a flag that is no longer the whole
            # story.
            "plc_segments_intact": not nesting_refinements,
            "vertical_refinement_band_m": (
                list(vertical_refinement_band_m)
                if vertical_refinement_band_m is not None else None
            ),
            "noding_serialization_quantum_m": _noding_serialization_quantum(
                outer_bounds, planar_quantum
            ),
            "planar_quantum_m": planar_quantum,
            "source_segment_max_length_m": (
                math.sqrt(2.0 * max_planar_area_m2)
                if max_planar_area_m2 is not None else None
            ),
            "prism_split": "freudenthal-3",
            "threads": 1,
            "triangle_coordinate_system": "physical_noded_source_points",
            "msh_version": 2.2,
        },
        "node_count": node_count,
        "edge_count": edge_count,
        "face_count": face_count,
        "tetrahedron_count": tetrahedron_count,
        "mesher": _meshpy_identity(),
        "mesh_sha256": file_sha256(path),
        "minimum_tetrahedron_determinant_m3": minimum_determinant,
        "source_identity": source_identity,
    }
    manifest = {
        "format": PLC_MESH_MANIFEST_FORMAT,
        "provenance": provenance,
        "provenance_sha256": canonical_sha256(provenance),
    }
    manifest_path = path.with_suffix(path.suffix + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    validate_palace_plc_mesh_manifest(manifest_path, mesh_path=path)
    return PalaceMeshResult(
        mesh_path=path,
        manifest_path=manifest_path,
        terminal_attributes=terminal_attributes,
        material_attributes=tuple(material_attributes),
        ground_attribute=9999,
        node_count=node_count,
        tetrahedron_count=tetrahedron_count,
    )


def _physical_elements(gmsh, dimension, element_type, attributes):
    element_tags, node_tags = gmsh.model.mesh.getElementsByType(element_type)
    element_tags = np.asarray(element_tags, dtype=np.int64)
    width = 4 if element_type == 4 else 3
    nodes = np.asarray(node_tags, dtype=np.int64).reshape((-1, width))
    order = np.argsort(element_tags)
    sorted_tags = element_tags[order]
    assigned = np.zeros(len(element_tags), dtype=np.int64)
    for attribute in attributes:
        for entity in gmsh.model.getEntitiesForPhysicalGroup(dimension, attribute):
            types, tag_groups, _ = gmsh.model.mesh.getElements(dimension, int(entity))
            for kind, tags in zip(types, tag_groups):
                if int(kind) != element_type:
                    continue
                tags = np.asarray(tags, dtype=np.int64)
                locations = np.searchsorted(sorted_tags, tags)
                if (np.any(locations >= len(sorted_tags))
                        or np.any(sorted_tags[locations] != tags)):
                    raise ValueError("physical group references unknown mesh elements")
                positions = order[locations]
                if np.any((assigned[positions] != 0)
                          & (assigned[positions] != attribute)):
                    raise ValueError("mesh element has conflicting physical groups")
                assigned[positions] = attribute
    if len(element_tags) == 0 or np.any(assigned == 0):
        raise ValueError("mesh elements lack complete physical attributes")
    return nodes, assigned


def _node_coordinates(gmsh, referenced_tags):
    tags, coordinates, _ = gmsh.model.mesh.getNodes()
    tags = np.asarray(tags, dtype=np.int64)
    coordinates = np.asarray(coordinates, dtype=float).reshape((-1, 3))
    order = np.argsort(tags)
    sorted_tags = tags[order]
    locations = np.searchsorted(sorted_tags, referenced_tags)
    if (np.any(locations >= len(sorted_tags))
            or np.any(sorted_tags[locations] != referenced_tags)):
        raise ValueError("mesh element references an unknown node")
    return coordinates[order[locations]]


def _nearest_level_indices(values, levels):
    right = np.clip(np.searchsorted(levels, values), 1, len(levels) - 1)
    left = right - 1
    use_right = np.abs(levels[right] - values) < np.abs(levels[left] - values)
    indices = np.where(use_right, right, left)
    nearest = levels[indices]
    serialization_bound = 8.0 * np.maximum(
        np.abs(np.spacing(values)), np.abs(np.spacing(nearest))
    )
    if np.any(np.abs(nearest - values) > serialization_bound):
        raise ValueError("Palace PLC tetrahedron uses an unknown z-plane")
    return indices


def _full_cell_materials(points, provenance, conductors, dielectrics):
    planar_quantum = provenance["mesh_parameters"]["planar_quantum_m"]
    xy_values, xy_inverse = np.unique(
        points[:, :, :2].reshape((-1, 2)), axis=0, return_inverse=True
    )
    tetrahedron_xy = np.sort(xy_inverse.reshape((-1, 4)), axis=1)
    keep = np.c_[
        np.ones(len(tetrahedron_xy), dtype=bool),
        tetrahedron_xy[:, 1:] != tetrahedron_xy[:, :-1],
    ]
    if np.any(keep.sum(axis=1) != 3):
        raise ValueError("Palace PLC tetrahedron is not an extruded planar cell")
    planar_triangles = tetrahedron_xy[keep].reshape((-1, 3))
    unique_triangles, triangle_inverse = np.unique(
        planar_triangles, axis=0, return_inverse=True
    )

    outer = BoxBounds(**provenance["outer_bounds"])
    edge_band_m, edge_area_m2 = _validate_conductor_edge_refinement(
        provenance["mesh_parameters"].get("conductor_edge_band_m"),
        provenance["mesh_parameters"].get("conductor_edge_max_planar_area_m2"),
        provenance["mesh_parameters"]["max_planar_area_m2"])
    edge_zone = (
        prep(_conductor_boundary(conductors).buffer(edge_band_m))
        if edge_band_m is not None else None
    )
    # Absent means a mesh written before nesting existed, which is exactly
    # the None case: equal-division levels.
    nesting_refinements = provenance["mesh_parameters"].get(
        "nesting_refinements")
    z_levels = np.asarray(_refine_levels(_coalesce_levels({
        *outer.minimum[2:3], *outer.maximum[2:3],
        *(value for item in conductors for value in (item.z_min, item.z_max)),
        *(value for item in dielectrics for value in _dielectric_z(item)),
    }, outer.lengths[2]), provenance["mesh_parameters"]["max_vertical_step_m"],
        _validate_vertical_refinement_band(
            provenance["mesh_parameters"].get("vertical_refinement_band_m")),
        # A nested mesh bisects its z gaps, so revalidating it against
        # equal-division levels would reconstruct a level set the mesh never
        # had and reject a sound mesh.
        nested=nesting_refinements is not None))
    node_level_indices = _nearest_level_indices(points[:, :, 2], z_levels)
    low_indices = node_level_indices.min(axis=1)
    high_indices = node_level_indices.max(axis=1)
    if (np.any(high_indices != low_indices + 1)
            or np.any(
                (node_level_indices != low_indices[:, None])
                & (node_level_indices != high_indices[:, None])
            )):
        raise ValueError("Palace PLC tetrahedron crosses a geometry z-plane")
    cells, cell_inverse = np.unique(
        np.c_[triangle_inverse, low_indices], axis=0, return_inverse=True
    )

    projected_cells = []
    edge_band_cells = edge_band_violations = 0
    actual_edges = set()
    for index, triangle in enumerate(unique_triangles):
        coordinates = [tuple(xy_values[value]) for value in triangle]
        polygon = Polygon(coordinates)
        if not polygon.is_valid or polygon.area <= 0.0:
            raise ValueError(
                "Palace PLC projected cell is degenerate: "
                f"triangle={index}, points={coordinates}"
            )
        projected_cells.append(polygon)
        max_area_m2 = provenance["mesh_parameters"]["max_planar_area_m2"]
        if max_area_m2 is not None:
            serialization_bound = 8.0 * max(
                math.ulp(polygon.area), math.ulp(max_area_m2)
            )
            if polygon.area > max_area_m2 + serialization_bound:
                raise ValueError(
                    "Palace PLC mesh exceeds the maximum planar triangle area"
                )
        if edge_zone is not None and edge_zone.intersects(polygon.centroid):
            edge_band_cells += 1
            if polygon.area > edge_area_m2 + 8.0 * max(
                    math.ulp(polygon.area), math.ulp(edge_area_m2)):
                edge_band_violations += 1
                if polygon.area > edge_area_m2 * MAX_EDGE_AREA_OVERSHOOT:
                    raise ValueError(
                        "Palace PLC cell exceeds the conductor edge planar "
                        f"area by more than {MAX_EDGE_AREA_OVERSHOOT:g}x "
                        "inside the refinement band"
                    )
        actual_edges.update(
            tuple(sorted((coordinates[left], coordinates[right])))
            for left, right in ((0, 1), (1, 2), (2, 0))
        )

    if edge_zone is not None:
        if not edge_band_cells:
            raise ValueError(
                "Palace PLC mesh records conductor edge refinement but has no "
                "cell inside the band, so the refinement cannot be verified"
            )
        violation_fraction = edge_band_violations / edge_band_cells
        if violation_fraction > MAX_EDGE_AREA_VIOLATION_FRACTION:
            raise ValueError(
                f"Palace PLC mesh leaves {violation_fraction * 100.0:.2f}% of "
                f"its {edge_band_cells} conductor edge band cells above the "
                "refinement area, so the recorded refinement was not applied"
            )

    source_segments = _geometry_lines(
        outer, conductors, dielectrics, planar_quantum,
        provenance["mesh_parameters"]["source_segment_max_length_m"],
    )
    if edge_band_m is not None:
        source_segments = _resplit_conductor_segments(
            source_segments, _conductor_boundary(conductors),
            math.sqrt(2.0 * edge_area_m2), planar_quantum,
        )
    expected_edges = {
        tuple(sorted((tuple(start), tuple(end))))
        for start, end in source_segments
    }
    coordinate_scale_m = max(
        abs(value) for value in (*outer.minimum[:2], *outer.maximum[:2]))
    coverage_grid_m = _coverage_grid_m(coordinate_scale_m, planar_quantum)
    missing_edges = expected_edges - actual_edges
    if missing_edges:
        actual_lines = tuple(LineString(edge) for edge in actual_edges)
        actual_tree = STRtree(actual_lines)
        uncovered = []
        for edge in missing_edges:
            line = LineString(edge)
            candidates = [
                actual_lines[int(index)] for index in actual_tree.query(line)
            ]
            # Residue length alone cannot answer this. A source segment
            # covered by mesh edges that are bent off it by a single ULP shares
            # only a measure-zero set with the straight line, so `difference`
            # returns *the whole segment* -- indistinguishable by length from a
            # segment that was dropped outright. Measured on the canary after
            # one uniform subdivision: 689 segments report 100% uncovered, and
            # the furthest any of them strays from the edges covering it is
            # 2.794e-17 m, one ULP of a 0.14 m coordinate. Segments Triangle
            # genuinely dropped were 1.9e-3 m away, nine orders further out.
            #
            # So the question is how far the remainder actually lies from the
            # edges, not how long it is.
            if not candidates:
                uncovered.append((line.length, line.length, edge))
                continue
            covering = union_all(candidates)
            remainder = line.difference(covering)
            if remainder.is_empty:
                continue
            # Only now is it worth snapping. The exact test is both cheaper and
            # more trustworthy, and on a mesh whose edges happen to land exactly
            # on the segments it answers every case on its own.
            if coverage_grid_m is not None:
                remainder = set_precision(line, coverage_grid_m).difference(
                    set_precision(covering, coverage_grid_m))
                if remainder.is_empty:
                    continue
            uncovered.append((remainder.length,
                              remainder.length / line.length, edge))
        if uncovered:
            worst = max(uncovered)
            # How far the segment actually strays from the edges near it is the
            # number that separates "dropped" from "covered but bent": the
            # measured values are ~1e-17 m for rounding and ~1e-3 m for a
            # segment Triangle genuinely lost. Computed once, for the worst
            # offender only, on a path that is already failing.
            line = LineString(worst[2])
            near = [actual_lines[int(index)]
                    for index in actual_tree.query(line)]
            stray = (float("inf") if not near else max(
                line.interpolate(step / 32.0, normalized=True).distance(
                    union_all(near)) for step in range(33)))
            raise ValueError(
                "Palace PLC mesh omits a noded source boundary segment: "
                f"missing={len(uncovered)} of {len(expected_edges)}, worst "
                f"leaves {worst[0]:.3e} m uncovered ({worst[1] * 100.0:.2f}% "
                f"of the segment) and strays {stray:.3e} m from the nearest "
                f"edges, sample={worst[2]}"
            )
    source_lines = tuple(LineString(segment) for segment in source_segments)
    source_tree = STRtree(source_lines)
    # Crossings are collected rather than raised on sight. One triangle and its
    # coordinates say nothing about whether the mesher ignored a constraint or
    # a single sliver grazes a boundary by a rounding error, and that is the
    # first question anyone asks. The happy path does the same work either way.
    crossing_tolerance_m = _crossing_tolerance_m(coordinate_scale_m)
    crossings = _boundary_crossings(
        projected_cells, source_lines, source_tree, crossing_tolerance_m,
        coverage_grid_m)
    if crossings:
        worst_depth_m = max(depth for _, depth, _ in crossings)
        worst = max(crossings, key=lambda item: item[1])
        raise ValueError(
            "Palace PLC tetrahedron crosses a noded source boundary: "
            f"crossings={len(crossings)} of {len(projected_cells)} cells, "
            f"worst penetrates {worst_depth_m:.3e} m "
            f"(tolerance {crossing_tolerance_m:.3e} m), "
            f"triangle={worst[0]}, points={worst[2]}"
        )

    conductor_polygons = tuple(_prism_polygon(item) for item in conductors)
    dielectric_polygons = tuple(_dielectric_polygon(item) for item in dielectrics)
    material_map = {
        name: attribute
        for (name, _), (_, attribute, _) in zip(
            _group_items(dielectrics), provenance["material_attributes"][1:]
        )
    }
    slab_regions = {}
    for low_index in np.unique(cells[:, 1]):
        z_mid = 0.5 * (z_levels[low_index] + z_levels[low_index + 1])
        records = []
        for name, _ in _group_items(conductors):
            active = [
                conductor_polygons[index]
                for index, item in enumerate(conductors)
                if item.name == name and item.z_min < z_mid < item.z_max
            ]
            if active:
                records.append(("conductor", name, union_all(active)))
        for name, _ in _group_items(dielectrics):
            active = [
                dielectric_polygons[index]
                for index, item in enumerate(dielectrics)
                if item.name == name
                and _dielectric_z(item)[0] < z_mid < _dielectric_z(item)[1]
            ]
            if active:
                records.append(("material", material_map[name], union_all(active)))
        geometries = tuple(item[2] for item in records)
        slab_regions[int(low_index)] = (records, STRtree(geometries))

    cell_materials = np.ones(len(cells), dtype=np.int64)
    for cell_index, (triangle_index, low_index) in enumerate(cells):
        location = projected_cells[triangle_index].centroid
        records, tree = slab_regions[int(low_index)]
        matches = [
            records[int(region_index)]
            for region_index in tree.query(location)
            if records[int(region_index)][2].covers(location)
        ]
        conductor_matches = [item for item in matches if item[0] == "conductor"]
        if conductor_matches:
            raise ValueError("Palace PLC mesh contains conductor tetrahedra")
        material_matches = [item[1] for item in matches if item[0] == "material"]
        if len(set(material_matches)) > 1:
            raise ValueError("Palace PLC dielectric regions overlap")
        if material_matches:
            cell_materials[cell_index] = material_matches[0]
    return cell_materials[cell_inverse]


def _validate_plc_mesh_topology(mesh_path, provenance, conductors, dielectrics):
    import gmsh

    initialized_here = not gmsh.isInitialized()
    if initialized_here:
        gmsh.initialize()
    try:
        gmsh.clear()
        gmsh.open(str(mesh_path))
        material_attributes = [item[1] for item in provenance["material_attributes"]]
        terminal_attributes = [item[1] for item in provenance["terminal_attributes"]]
        tetrahedra, actual_materials = _physical_elements(
            gmsh, 3, 4, material_attributes
        )
        triangles, triangle_markers = _physical_elements(
            gmsh, 2, 2, [*terminal_attributes, provenance["ground_attribute"]]
        )
        tetrahedron_points = _node_coordinates(gmsh, tetrahedra)
        minimum_determinant = math.inf
        chunk_size = 100_000
        for start in range(0, len(tetrahedra), chunk_size):
            stop = min(start + chunk_size, len(tetrahedra))
            points = tetrahedron_points[start:stop]
            determinants = _tetrahedron_determinants(points)
            if not np.isfinite(determinants).all() or np.any(determinants <= 0.0):
                raise ValueError("Palace PLC mesh has a nonpositive tetrahedron")
            minimum_determinant = min(minimum_determinant, float(determinants.min()))
        expected_materials = _full_cell_materials(
            tetrahedron_points, provenance, conductors, dielectrics
        )
        if not np.array_equal(expected_materials, actual_materials):
            raise ValueError("Palace PLC tetrahedron material is misclassified")
        witness = provenance["minimum_tetrahedron_determinant_m3"]
        if not np.isclose(minimum_determinant, witness, rtol=1e-10, atol=0.0):
            raise ValueError(
                "Palace PLC Jacobian witness differs from mesh bytes: "
                f"actual={minimum_determinant}, expected={witness}"
            )

        faces = np.concatenate((
            tetrahedra[:, (1, 2, 3)], tetrahedra[:, (0, 3, 2)],
            tetrahedra[:, (0, 1, 3)], tetrahedra[:, (0, 2, 1)],
        ))
        faces.sort(axis=1)
        face_order = np.lexsort((faces[:, 2], faces[:, 1], faces[:, 0]))
        faces = faces[face_order]
        starts = np.r_[
            0, np.flatnonzero(np.any(faces[1:] != faces[:-1], axis=1)) + 1
        ]
        counts = np.diff(np.r_[starts, len(faces)])
        if np.any((counts != 1) & (counts != 2)):
            raise ValueError("Palace PLC tetrahedral faces are non-manifold")
        boundary_faces = faces[starts[counts == 1]]
        triangles = np.sort(triangles, axis=1)
        triangle_order = np.lexsort(
            (triangles[:, 2], triangles[:, 1], triangles[:, 0])
        )
        triangles = triangles[triangle_order]
        triangle_markers = triangle_markers[triangle_order]
        if (len(triangles) != len(boundary_faces)
                or not np.array_equal(triangles, boundary_faces)):
            raise ValueError("Palace PLC marked boundaries do not close the domain")
        if set(map(int, triangle_markers)) != {
                *terminal_attributes, provenance["ground_attribute"]}:
            raise ValueError("Palace PLC boundary attributes are incomplete")
    except Exception as error:
        if isinstance(error, ValueError):
            raise
        raise ValueError(f"invalid Palace PLC topology: {error}") from error
    finally:
        gmsh.clear()
        if initialized_here:
            gmsh.finalize()


def validate_palace_plc_mesh_manifest(path, *, mesh_path=None):
    path = Path(path).resolve()
    try:
        raw = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid Palace PLC mesh manifest: {error}") from error
    if not isinstance(raw, dict) or raw.get("format") != PLC_MESH_MANIFEST_FORMAT:
        raise ValueError("unsupported Palace PLC mesh manifest format")
    provenance = raw.get("provenance")
    if (not isinstance(provenance, dict)
            or raw.get("provenance_sha256") != canonical_sha256(provenance)):
        raise ValueError("Palace PLC mesh provenance hash mismatch")
    required = {
        "conductors", "dielectrics", "edge_count", "face_count",
        "ground_attribute", "material_attributes", "mesh_parameters",
        "mesh_sha256", "mesher", "minimum_tetrahedron_determinant_m3",
        "node_count", "outer_bounds",
        "outer_permittivity", "reference_semantics", "source_identity",
        "terminal_attributes", "tetrahedron_count",
    }
    if set(provenance) != required:
        raise ValueError("Palace PLC mesh provenance schema mismatch")
    if mesh_path is None:
        mesh_path = Path(str(path).removesuffix(".manifest.json"))
    mesh_path = Path(mesh_path).resolve()
    if provenance.get("mesh_sha256") != file_sha256(mesh_path):
        raise ValueError("Palace PLC mesh hash mismatch")
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
        raise ValueError(f"invalid Palace PLC geometry: {error}") from error
    if not conductors:
        raise ValueError("Palace PLC mesh requires conductors")
    conductor_groups = _group_items(conductors)
    dielectric_groups = _group_items(dielectrics)
    terminals = provenance["terminal_attributes"]
    materials = provenance["material_attributes"]
    expected_terminal_names = [name for name, _ in conductor_groups]
    expected_material_names = ["outer", *(name for name, _ in dielectric_groups)]
    if (not isinstance(terminals, list) or len(terminals) != len(conductor_groups)
            or any(not isinstance(item, list) or len(item) != 2
                   or not isinstance(item[0], str) or type(item[1]) is not int
                   or item[1] <= 0 for item in terminals)
            or [item[0] for item in terminals] != expected_terminal_names
            or len({item[1] for item in terminals}) != len(terminals)):
        raise ValueError("Palace PLC terminal attributes are invalid")
    if (not isinstance(materials, list) or len(materials) != len(expected_material_names)
            or any(not isinstance(item, list) or len(item) != 3
                   or not isinstance(item[0], str) or type(item[1]) is not int
                   or item[1] <= 0 or isinstance(item[2], bool)
                   or not isinstance(item[2], (int, float))
                   or not math.isfinite(item[2]) or item[2] <= 0.0
                   for item in materials)
            or [item[0] for item in materials] != expected_material_names
            or len({item[1] for item in materials}) != len(materials)):
        raise ValueError("Palace PLC material attributes are invalid")
    outer_permittivity = provenance["outer_permittivity"]
    if (isinstance(outer_permittivity, bool)
            or not isinstance(outer_permittivity, (int, float))
            or not math.isfinite(outer_permittivity) or outer_permittivity <= 0.0
            or not canonical_equal(materials[0][2], outer_permittivity)):
        raise ValueError("Palace PLC outer permittivity is invalid")
    ground = provenance["ground_attribute"]
    occupied = {*(item[1] for item in terminals), *(item[1] for item in materials)}
    if type(ground) is not int or ground <= 0 or ground in occupied:
        raise ValueError("Palace PLC ground attribute is invalid")
    if not canonical_equal(provenance["reference_semantics"], {
            "kind": "finite_outer_dirichlet_approximation",
            "convergence_ladder_required": True,
            "not_a_circuit_node": True,
    }):
        raise ValueError("Palace PLC reference semantics are invalid")
    for item in (*conductors, *dielectrics):
        z_min, z_max = ((item.bounds.minimum[2], item.bounds.maximum[2])
                        if isinstance(item, DielectricBox)
                        else (item.z_min, item.z_max))
        if not outer.minimum[2] < z_min < z_max < outer.maximum[2]:
            raise ValueError("Palace PLC volume lies outside the outer bounds")
    determinant = provenance["minimum_tetrahedron_determinant_m3"]
    if (isinstance(determinant, bool)
            or not isinstance(determinant, (int, float))
            or not math.isfinite(determinant) or determinant <= 0.0):
        raise ValueError("Palace PLC tetrahedral Jacobian witness is invalid")
    source_geometry = _validate_source_identity(provenance["source_identity"])
    if source_geometry is not None and (
            not canonical_equal(
                [asdict(item) for item in source_geometry.conductors],
                provenance["conductors"],
            )
            or not canonical_equal(
                [asdict(item) for item in source_geometry.dielectrics],
                provenance["dielectrics"],
            )
            or not canonical_equal(
                asdict(source_geometry.outer_bounds), provenance["outer_bounds"]
            )):
        raise ValueError("Palace PLC source reconstruction differs from mesh geometry")
    parameters = provenance.get("mesh_parameters")
    # The nesting keys are optional: a mesh written before nesting existed
    # omits them, and their absence unambiguously means an unnested mesh.
    # Everything else is still an exact set, so an unknown or missing
    # parameter is still refused.
    if (not isinstance(parameters, dict)
            or not _nesting_parameters_valid(parameters)
            or set(parameters) - {
                "nesting_refinements", "plc_segments_intact"} != {
            "allow_boundary_steiner", "allow_volume_steiner", "backend",
            "conductor_edge_band_m", "conductor_edge_max_planar_area_m2",
            "max_planar_area_m2", "max_vertical_step_m", "msh_version",
            "noding_serialization_quantum_m", "planar_quantum_m", "prism_split",
            "source_segment_max_length_m", "threads",
            "triangle_coordinate_system", "vertical_refinement_band_m"}
            or parameters["backend"] != "meshpy-triangle-extrusion"
            or parameters["allow_boundary_steiner"] is not False
            or parameters["allow_volume_steiner"] is not False
            or parameters["prism_split"] != "freudenthal-3"
            or parameters["triangle_coordinate_system"] != (
                "physical_noded_source_points"
            )
            or type(parameters["threads"]) is not int or parameters["threads"] != 1
            or not canonical_equal(parameters["msh_version"], 2.2)
            or not canonical_equal(
                parameters["planar_quantum_m"],
                _planar_quantum(outer, provenance["source_identity"]),
            )
            or not canonical_equal(
                parameters["noding_serialization_quantum_m"],
                _noding_serialization_quantum(
                    outer, parameters["planar_quantum_m"]
                ),
            )
            or not canonical_equal(
                parameters["source_segment_max_length_m"],
                math.sqrt(2.0 * parameters["max_planar_area_m2"])
                if parameters["max_planar_area_m2"] is not None else None,
            )
            or any(value is not None
                   and (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) or value <= 0.0)
                   for value in (parameters["max_planar_area_m2"],
                                 parameters["max_vertical_step_m"],
                                 parameters["noding_serialization_quantum_m"],
                                 parameters["planar_quantum_m"],
                                 parameters["source_segment_max_length_m"]))):
        raise ValueError("Palace PLC mesh parameters are invalid")
    try:
        _validate_vertical_refinement_band(parameters["vertical_refinement_band_m"])
        _validate_conductor_edge_refinement(
            parameters["conductor_edge_band_m"],
            parameters["conductor_edge_max_planar_area_m2"],
            parameters["max_planar_area_m2"])
    except ValueError:
        raise ValueError("Palace PLC mesh parameters are invalid") from None
    mesher = provenance.get("mesher")
    if (not isinstance(mesher, dict) or set(mesher) != {
            "backend", "native_extension", "native_extension_sha256",
            "python_package", "python_package_sha256", "version"}
            or mesher["backend"] != "meshpy-triangle-extrusion"
            or not isinstance(mesher["version"], str) or not mesher["version"]
            or not isinstance(mesher["python_package"], str)
            or not isinstance(mesher["native_extension"], str)):
        raise ValueError("Palace PLC mesher identity is invalid")
    for file_key, hash_key in (
            ("python_package", "python_package_sha256"),
            ("native_extension", "native_extension_sha256")):
        if file_sha256(mesher[file_key]) != mesher[hash_key]:
            raise ValueError(f"Palace PLC {file_key} hash mismatch")
    if any(type(provenance.get(name)) is not int or provenance[name] <= 0
           for name in (
               "node_count", "edge_count", "face_count", "tetrahedron_count"
           )):
        raise ValueError("Palace PLC mesh counts are invalid")
    validate_palace_mesh_content(mesh_path, provenance)
    _validate_plc_mesh_topology(mesh_path, provenance, conductors, dielectrics)
    return raw
