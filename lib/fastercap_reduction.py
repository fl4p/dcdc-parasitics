from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path

import numpy as np
from shapely.geometry import Polygon
from shapely.ops import unary_union

if __package__:
    from .fastercap import ConductorSurface
    from .fastercap_diagnostics import validate_deck_manifest
else:
    from fastercap import ConductorSurface
    from fastercap_diagnostics import validate_deck_manifest


QUALIFICATION_ID = "parallel-plate-air-effective-thickness-v1"
QUALIFIED_SOURCE_THICKNESS_M = 35e-6
QUALIFIED_EFFECTIVE_THICKNESS_M = 34e-6
QUALIFIED_PLATE_SIZE_M = 1e-3
QUALIFIED_SOURCE_GAP_M = 100e-6
QUALIFIED_ANCHOR = "midplane"
QUALIFICATION_TOLERANCE_M = 5e-12


@dataclass(frozen=True)
class ThicknessReductionRequest:
    representation: str = "physical"
    effective_thickness_m: float | None = None
    anchor: str = "midplane"

    def __post_init__(self):
        if self.representation not in ("physical", "effective_thickness"):
            raise ValueError(
                "representation must be physical or effective_thickness; "
                "thin sheets are unsupported"
            )
        if self.anchor not in ("midplane", "top", "bottom"):
            raise ValueError("anchor must be midplane, top, or bottom")
        if self.representation == "physical":
            if self.effective_thickness_m is not None:
                raise ValueError(
                    "physical representation cannot set effective thickness"
                )
            return
        thickness = self.effective_thickness_m
        if thickness is None or not math.isfinite(thickness) or thickness <= 0.0:
            raise ValueError(
                "effective_thickness requires a finite positive thickness"
            )


@dataclass(frozen=True)
class ReductionContext:
    source_mode: str
    material_scope: str
    fixture_id: str | None = None

    def __post_init__(self):
        if self.source_mode not in ("synthetic_fixture", "filled_zones_only"):
            raise ValueError("unsupported reduction source mode")
        if self.material_scope not in ("air_only", "full_stackup"):
            raise ValueError("material scope must be air_only or full_stackup")
        if self.fixture_id is not None and self.source_mode != "synthetic_fixture":
            raise ValueError("only a synthetic fixture may declare a fixture ID")


@dataclass(frozen=True)
class ThicknessReductionResult:
    conductors: tuple[ConductorSurface, ...]
    provenance: dict


@dataclass(frozen=True)
class ComponentExpansion:
    conductors: tuple[ConductorSurface, ...]
    assignments: dict[str, str]
    group_order: tuple[str, ...]


def expand_conductor_components(conductors):
    expanded = []
    assignments = {}
    group_order = []
    for conductor in conductors:
        group_order.append(conductor.name)
        for index, part in enumerate(conductor.parts, 1):
            name = f"{conductor.name}__component_{index:03d}"
            expanded.append(ConductorSurface(
                name,
                part,
                relative_permittivity=conductor.relative_permittivity,
            ))
            assignments[name] = conductor.name
    if not expanded:
        raise ValueError("at least one conductor component is required")
    return ComponentExpansion(
        tuple(expanded), assignments, tuple(group_order)
    )


def thickness_bounds(midplane_m, source_thickness_m, request):
    source_thickness_m = float(source_thickness_m)
    midplane_m = float(midplane_m)
    if (not math.isfinite(source_thickness_m)
            or source_thickness_m <= 0.0
            or not math.isfinite(midplane_m)):
        raise ValueError("midplane and source thickness must be finite and valid")
    if request.representation == "physical":
        thickness = source_thickness_m
    else:
        thickness = request.effective_thickness_m
        if thickness >= source_thickness_m:
            raise ValueError(
                "effective thickness must be smaller than source thickness"
            )
    source_min = midplane_m - source_thickness_m / 2.0
    source_max = midplane_m + source_thickness_m / 2.0
    if request.anchor == "midplane":
        return midplane_m - thickness / 2.0, midplane_m + thickness / 2.0
    if request.anchor == "top":
        return source_max - thickness, source_max
    return source_min, source_min + thickness


def _surface_points(surface):
    return np.asarray(
        [point for panel in surface.panels for point in panel], dtype=float
    )


def _validate_closed_surface(surface):
    edge_uses = {}
    for panel in surface.panels:
        points = [tuple(float(value) for value in point) for point in panel]
        for index, first in enumerate(points):
            second = points[(index + 1) % len(points)]
            key = tuple(sorted((first, second)))
            edge_uses.setdefault(key, []).append((first, second))
    invalid = []
    for edge, uses in edge_uses.items():
        if len(uses) != 2 or uses[0] != (uses[1][1], uses[1][0]):
            invalid.append(edge)
    if invalid:
        raise ValueError(
            f"conductor {surface.name!r} is not a closed orientable surface"
        )


def _surface_slabs(surface):
    levels = np.unique(_surface_points(surface)[:, 2])
    if len(levels) % 2:
        raise ValueError(
            f"conductor {surface.name!r} does not contain paired extrusion faces"
        )
    slabs = []
    for index in range(0, len(levels), 2):
        lower = float(levels[index])
        upper = float(levels[index + 1])
        if upper <= lower:
            raise ValueError(f"conductor {surface.name!r} has invalid z bounds")
        slabs.append((lower, upper))
    return tuple(slabs)


def _transform_surface(surface, request):
    slabs = _surface_slabs(surface)
    mapping = {}
    thicknesses = []
    maximum_displacement = 0.0
    for lower, upper in slabs:
        source_thickness = upper - lower
        candidate_lower, candidate_upper = thickness_bounds(
            (lower + upper) / 2.0, source_thickness, request
        )
        mapping[lower] = candidate_lower
        mapping[upper] = candidate_upper
        thicknesses.append(source_thickness)
        maximum_displacement = max(
            maximum_displacement,
            abs(candidate_lower - lower),
            abs(candidate_upper - upper),
        )
    parts = []
    for part in surface.parts:
        transformed_part = []
        for panel in part:
            transformed = []
            for point in panel:
                z = float(point[2])
                if z not in mapping:
                    raise ValueError(
                        f"conductor {surface.name!r} contains a non-face z coordinate"
                    )
                transformed.append((
                    float(point[0]), float(point[1]), mapping[z]
                ))
            transformed_part.append(tuple(transformed))
        parts.append(tuple(transformed_part))
    panels = tuple(panel for part in parts for panel in part)
    return (
        ConductorSurface(
            surface.name,
            panels,
            relative_permittivity=surface.relative_permittivity,
            parts=tuple(parts),
        ),
        tuple(thicknesses),
        maximum_displacement,
    )


def _xy_bounds(surface):
    points = _surface_points(surface)
    return (
        float(np.min(points[:, 0])), float(np.max(points[:, 0])),
        float(np.min(points[:, 1])), float(np.max(points[:, 1])),
    )


def _projected_top_area(surface):
    points = _surface_points(surface)
    top = float(np.max(points[:, 2]))
    area = 0.0
    for panel in surface.panels:
        coordinates = np.asarray(panel, dtype=float)
        if not np.all(coordinates[:, 2] == top):
            continue
        x = coordinates[:, 0]
        y = coordinates[:, 1]
        area += abs(float(np.dot(x, np.roll(y, -1))
                          - np.dot(y, np.roll(x, -1)))) / 2.0
    return area


def _surface_prisms(surface):
    prisms = []
    for lower, upper in _surface_slabs(surface):
        polygons = []
        for panel in surface.panels:
            coordinates = np.asarray(panel, dtype=float)
            if not np.all(coordinates[:, 2] == upper):
                continue
            polygons.append(Polygon(coordinates[:, :2]))
        projection = unary_union(polygons)
        if projection.is_empty or not projection.is_valid:
            raise ValueError(
                f"conductor {surface.name!r} has invalid projected extrusion"
            )
        prisms.append((projection, lower, upper))
    return tuple(prisms)


def _prism_clearance(first, second):
    planar = first[0].distance(second[0])
    vertical = max(first[1] - second[2], second[1] - first[2], 0.0)
    return math.hypot(planar, vertical)


def _minimum_clearance(conductors):
    prisms = [_surface_prisms(surface) for surface in conductors]
    values = [
        _prism_clearance(first_prism, second_prism)
        for index, first in enumerate(prisms)
        for second in prisms[index + 1:]
        for first_prism in first
        for second_prism in second
    ]
    return None if not values else min(values)


def _near(value, expected):
    return math.isclose(
        value, expected, rel_tol=0.0, abs_tol=QUALIFICATION_TOLERANCE_M
    )


def _inside_qualified_envelope(
        source, request, context, source_clearance):
    if request.representation != "effective_thickness":
        return False
    if (context.source_mode != "synthetic_fixture"
            or context.material_scope != "air_only"
            or context.fixture_id != QUALIFICATION_ID):
        return False
    if request.anchor != QUALIFIED_ANCHOR:
        return False
    if not _near(
        request.effective_thickness_m, QUALIFIED_EFFECTIVE_THICKNESS_M
    ):
        return False
    if len(source) != 2 or source_clearance is None:
        return False
    if not _near(source_clearance, QUALIFIED_SOURCE_GAP_M):
        return False
    bounds = [_xy_bounds(surface) for surface in source]
    if any(surface.relative_permittivity != 1.0 for surface in source):
        return False
    if any(len(_surface_slabs(surface)) != 1 for surface in source):
        return False
    if any(not _near(
            _surface_slabs(surface)[0][1] - _surface_slabs(surface)[0][0],
            QUALIFIED_SOURCE_THICKNESS_M) for surface in source):
        return False
    expected_area = QUALIFIED_PLATE_SIZE_M ** 2
    if any(not math.isclose(
            _projected_top_area(surface), expected_area,
            rel_tol=1e-9, abs_tol=1e-18) for surface in source):
        return False
    if any(not _near(x_max - x_min, QUALIFIED_PLATE_SIZE_M)
           or not _near(y_max - y_min, QUALIFIED_PLATE_SIZE_M)
           for x_min, x_max, y_min, y_max in bounds):
        return False
    return all(_near(first, second) for first, second in zip(bounds[0], bounds[1]))


def apply_thickness_reduction(conductors, request, context):
    source = tuple(conductors)
    if not source:
        raise ValueError("at least one conductor is required")
    if not isinstance(request, ThicknessReductionRequest):
        raise TypeError("request must be a ThicknessReductionRequest")
    if not isinstance(context, ReductionContext):
        raise TypeError("context must be a ReductionContext")

    transformed = []
    source_thicknesses = {}
    maximum_displacement = 0.0
    for surface in source:
        _validate_closed_surface(surface)
        result, thicknesses, displacement = _transform_surface(surface, request)
        transformed.append(result)
        source_thicknesses[surface.name] = thicknesses
        maximum_displacement = max(maximum_displacement, displacement)
    transformed = tuple(transformed)
    source_clearance = _minimum_clearance(source)
    candidate_clearance = _minimum_clearance(transformed)
    if source_clearance is not None and source_clearance <= 0.0:
        raise ValueError("source conductors intersect or touch")
    if candidate_clearance is not None and candidate_clearance <= 0.0:
        raise ValueError("candidate conductors intersect or touch")
    clearance_change = (
        None if source_clearance is None or candidate_clearance is None
        else abs(candidate_clearance - source_clearance)
    )
    allowed_clearance_change = (
        None if source_clearance is None
        else min(1e-6, 0.01 * source_clearance)
    )
    if (clearance_change is not None
            and allowed_clearance_change is not None
            and clearance_change > allowed_clearance_change
            + QUALIFICATION_TOLERANCE_M):
        raise ValueError(
            "candidate clearance change exceeds min(1 um, 1% source gap)"
        )
    fixture_qualified = _inside_qualified_envelope(
        source, request, context, source_clearance
    )
    provenance = {
        "format": "dcdc-fastercap-thickness-reduction-v1",
        "representation": request.representation,
        "source_mode": context.source_mode,
        "material_scope": context.material_scope,
        "fixture_id": context.fixture_id,
        "effective_thickness_m": request.effective_thickness_m,
        "anchor": request.anchor,
        "source_thickness_m": {
            name: list(values) for name, values in source_thicknesses.items()
        },
        "simplification": {"method": "none"},
        "maximum_area_relative_error": 0.0,
        "source_minimum_clearance_m": source_clearance,
        "candidate_minimum_clearance_m": candidate_clearance,
        "maximum_clearance_error_m": clearance_change,
        "allowed_clearance_error_m": allowed_clearance_change,
        "maximum_planar_boundary_displacement_m": 0.0,
        "maximum_surface_displacement_m": maximum_displacement,
        "source_panel_count": {
            surface.name: len(surface.panels) for surface in source
        },
        "candidate_panel_count": {
            surface.name: len(surface.panels) for surface in transformed
        },
        "source_component_count": {
            surface.name: len(surface.parts) for surface in source
        },
        "candidate_component_count": {
            surface.name: len(surface.parts) for surface in transformed
        },
        "qualification_id": QUALIFICATION_ID,
        "qualification_state": (
            "fixture_qualified" if fixture_qualified
            else "outside_fixture_envelope"
        ),
        "qualification_scope": {
            "medium": "air",
            "conductor_count": 2,
            "plate_size_m": QUALIFIED_PLATE_SIZE_M,
            "source_thickness_m": QUALIFIED_SOURCE_THICKNESS_M,
            "effective_thickness_m": QUALIFIED_EFFECTIVE_THICKNESS_M,
            "source_face_gap_m": QUALIFIED_SOURCE_GAP_M,
            "anchor": QUALIFIED_ANCHOR,
        },
        "physical_validation_authorized": False,
        "lifecycle_ceiling": "numerically_converged_diagnostic",
    }
    return ThicknessReductionResult(transformed, provenance)


def representation_label(request):
    if request.representation == "physical":
        return "physical"
    micrometres = request.effective_thickness_m * 1e6
    text = f"{micrometres:.9g}".replace(".", "p")
    return f"effective_{text}um_{request.anchor}"


def diagnostic_artifact_stem(context, request, conductor_policy="grouped"):
    if not isinstance(context, ReductionContext):
        raise TypeError("context must be a ReductionContext")
    if conductor_policy not in ("grouped", "base_components"):
        raise ValueError("unsupported conductor policy")
    source = context.source_mode.replace("_only", "")
    policy = "" if conductor_policy == "grouped" else "_base_components"
    return (
        f"{source}_{representation_label(request)}{policy}_"
        f"{context.material_scope}_diagnostic"
    )


def _sha256_file(path):
    with open(path, "rb") as stream:
        return hashlib.sha256(stream.read()).hexdigest()


def load_content_addressed_manifest(path):
    path = Path(path)
    try:
        document = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid content-addressed manifest: {error}") from error
    if not isinstance(document, dict):
        raise ValueError("content-addressed manifest must be an object")
    content_id = document.get("content_id_sha256")
    if not isinstance(content_id, str) or len(content_id) != 64:
        raise ValueError("content-addressed manifest has no valid content ID")
    payload = dict(document)
    payload.pop("content_id_sha256")
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    if hashlib.sha256(canonical).hexdigest() != content_id:
        raise ValueError("content-addressed manifest does not match its hash")
    if not path.name.endswith(f".{content_id}.json"):
        raise ValueError("content-addressed manifest filename does not match hash")
    return document


def load_geometry_manifest(path):
    document = load_content_addressed_manifest(path)
    if document.get("format") != "dcdc-fastercap-geometry-v1":
        raise ValueError("unsupported FasterCap geometry manifest format")
    deck_path = Path(document.get("deck", ""))
    deck_manifest_path = Path(document.get("deck_manifest", ""))
    if not deck_path.is_absolute() or not deck_manifest_path.is_absolute():
        raise ValueError("geometry manifest artifact paths must be absolute")
    expected_manifest = deck_path.with_name(f"{deck_path.name}.manifest.json")
    if deck_manifest_path != expected_manifest:
        raise ValueError("geometry manifest does not name the deck's manifest")
    try:
        deck_hash = _sha256_file(deck_path)
        deck_manifest_hash = _sha256_file(deck_manifest_path)
    except OSError as error:
        raise ValueError(f"geometry manifest artifact is unavailable: {error}") from error
    if deck_hash != document.get("deck_sha256"):
        raise ValueError("geometry manifest deck hash mismatch")
    if deck_manifest_hash != document.get("deck_manifest_sha256"):
        raise ValueError("geometry manifest deck-manifest hash mismatch")
    validated = validate_deck_manifest(
        deck_manifest_path,
        deck_sha256=document["deck_sha256"],
        deck_path=deck_path,
    )
    provenance = document.get("reduction")
    if (validated.raw.get("provenance") != provenance
            or validated.raw.get("provenance_sha256")
            != document.get("reduction_provenance_sha256")):
        raise ValueError("geometry and deck reduction provenance do not match")
    return document


def write_content_addressed_manifest(directory, stem, payload):
    if not isinstance(payload, dict):
        raise TypeError("manifest payload must be a dictionary")
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    content_id = hashlib.sha256(canonical).hexdigest()
    document = dict(payload)
    document["content_id_sha256"] = content_id
    path = Path(directory) / f"{stem}.{content_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)
    return path
