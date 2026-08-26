"""Shared stdlib-only schema helpers for the KiCad Palace boundary."""
import math

from kicad_fastercap_schema import parse_group


PCB_VOLUME_DUMP_FORMAT = "dcdc-kicad-palace-volumes-v1"

# A board region thinner than this fraction of its own Edge.Cuts extent is
# rejected. This is a sanity floor against the stroked-outline failure mode, not
# a judgement about board shape: measured, a stroked outline fills 0.47%
# (canary) to 0.68% (Fugu2), while an enclosed one fills 99.5% to 100.0%. A
# genuinely sparse board trips this too, which is the intended direction -- the
# operator sees the message rather than a silently hollow dielectric.
MIN_OUTLINE_FILL_FRACTION = 0.10


def _ring_points(ring, what):
    try:
        points = [(float(x), float(y)) for x, y in ring]
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"KiCad board outline {what} is not a list of coordinate pairs"
        ) from error
    if len(points) < 3:
        raise ValueError(f"KiCad board outline {what} has fewer than 3 points")
    if any(not math.isfinite(value) for point in points for value in point):
        raise ValueError(f"KiCad board outline {what} has non-finite coordinates")
    return points


def _outline_rings(outlines):
    """Every ring of every outline, as (points, is_hole), validated.

    Raises rather than skipping anything it cannot read: a board region that
    cannot be measured must not be reported as fine.
    """
    if not isinstance(outlines, (list, tuple)) or not outlines:
        raise ValueError("KiCad board has no closed Edge.Cuts outline")
    rings = []
    for polygon in outlines:
        if not isinstance(polygon, dict) or "shell" not in polygon:
            raise ValueError("KiCad board outline entry has no shell")
        rings.append((_ring_points(polygon["shell"], "shell"), False))
        holes = polygon.get("holes", ())
        if not isinstance(holes, (list, tuple)):
            raise ValueError("KiCad board outline holes are not a list")
        rings.extend((_ring_points(hole, "hole"), True) for hole in holes)
    return rings


def _ring_area_mm2(points):
    total = 0.0
    count = len(points)
    for index in range(count):
        x_start, y_start = points[index]
        x_end, y_end = points[(index + 1) % count]
        total += x_start * y_end - x_end * y_start
    return abs(total) / 2.0


def outline_area_mm2(outlines):
    """Material area enclosed by the board outline, holes removed."""
    total = 0.0
    for points, is_hole in _outline_rings(outlines):
        area = _ring_area_mm2(points)
        total += -area if is_hole else area
    return total


def outline_bounding_area_mm2(outlines):
    """Area of the outline's own bounding box.

    Usable wherever the board is not open in KiCad. For a stroked outline this
    is the frame's extent, which is the board's extent to within the stroke
    width, so the fill fraction it yields still exposes the defect.
    """
    points = [point for ring, _ in _outline_rings(outlines) for point in ring]
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    return (max(xs) - min(xs)) * (max(ys) - min(ys))


def check_board_outline_fill(outlines, bounding_area_mm2):
    """Fail closed when the board region is implausibly thin for its extent.

    The failure this exists for is silent. Stroking the Edge.Cuts graphics
    rather than enclosing them yields a non-empty, valid, correctly-wound
    picture frame, and every downstream stage accepts it: the mesh builds, the
    solve converges, and the only symptom is that the dielectric is missing and
    the board interior is solved as air. Nothing else in the pipeline looks at
    how much area the outline encloses.
    """
    if (not isinstance(bounding_area_mm2, (int, float))
            or isinstance(bounding_area_mm2, bool)
            or not math.isfinite(bounding_area_mm2)
            or bounding_area_mm2 <= 0.0):
        raise ValueError(
            "KiCad board outline has no measurable extent, so its fill "
            "fraction cannot be evaluated"
        )
    area_mm2 = outline_area_mm2(outlines)
    if not math.isfinite(area_mm2) or area_mm2 <= 0.0:
        raise ValueError("KiCad board outline encloses no area")
    fraction = area_mm2 / bounding_area_mm2
    if fraction < MIN_OUTLINE_FILL_FRACTION:
        raise ValueError(
            f"KiCad board outline encloses {area_mm2:.3f} mm2, only "
            f"{fraction * 100.0:.2f}% of its {bounding_area_mm2:.3f} mm2 "
            f"Edge.Cuts bounding box, below the "
            f"{MIN_OUTLINE_FILL_FRACTION * 100.0:.0f}% floor. This is what a "
            "stroked rather than enclosed Edge.Cuts outline looks like: check "
            "that the board region, not the outline graphics, was polygonised."
        )
    return fraction


__all__ = [
    "MIN_OUTLINE_FILL_FRACTION",
    "PCB_VOLUME_DUMP_FORMAT",
    "check_board_outline_fill",
    "outline_area_mm2",
    "outline_bounding_area_mm2",
    "parse_group",
]
