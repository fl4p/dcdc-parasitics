#!/usr/bin/env python3
"""Solver-neutral electrostatic qualification fixtures."""
from dataclasses import dataclass

from shapely.geometry import Polygon, box


SOURCE_THICKNESS_M = 35e-6


@dataclass(frozen=True)
class ElectrostaticFixture:
    name: str
    polygons: tuple[Polygon, ...]
    midplanes_m: tuple[float, ...]
    source_thickness_m: float
    relative_permittivity: float
    dielectric_bounds_m: tuple[tuple[float, float, float],
                               tuple[float, float, float]] | None
    scope: str


def electrostatic_fixtures():
    square = box(-0.5e-3, -0.5e-3, 0.5e-3, 0.5e-3)
    vertical_gap = 100e-6
    upper_midplane = SOURCE_THICKNESS_M + vertical_gap
    parallel = ElectrostaticFixture(
        "parallel_plate_air",
        (square, square),
        (0.0, upper_midplane),
        SOURCE_THICKNESS_M,
        1.0,
        None,
        "overlapping parallel rectangular plates in air; 1 mm square; "
        "100 um physical face gap",
    )
    pcb_left = Polygon((
        (-2e-3, -1e-3), (-1e-3, -1e-3), (-0.2e-3, -1e-3),
        (-0.2e-3, -0.5e-3), (-0.2e-3, 0.0), (-0.8e-3, 0.0),
        (-0.8e-3, 0.5e-3), (-0.8e-3, 1e-3),
        (-1.4e-3, 1e-3), (-2e-3, 1e-3),
    ))
    pcb_right = Polygon((
        (0.2e-3, -1e-3), (1e-3, -1e-3), (2e-3, -1e-3),
        (2e-3, 0.0), (2e-3, 1e-3),
        (1e-3, 1e-3), (0.2e-3, 1e-3), (0.2e-3, 0.0),
    ))
    pcb = ElectrostaticFixture(
        "pcb_like_coplanar_air",
        (pcb_left, pcb_right),
        (0.0, 0.0),
        SOURCE_THICKNESS_M,
        1.0,
        None,
        "coplanar L-shape and rectangle in air; 400 um lateral clearance; "
        "redundant collinear boundary segmentation at 0.5-1 mm spacing",
    )
    material_square = box(-5e-3, -5e-3, 5e-3, 5e-3)
    material_gap = 500e-6
    material_upper_midplane = SOURCE_THICKNESS_M + material_gap
    material = ElectrostaticFixture(
        "parallel_plate_enclosed_fr4",
        (material_square, material_square),
        (0.0, material_upper_midplane),
        SOURCE_THICKNESS_M,
        4.2,
        ((-7e-3, -7e-3, -1e-3), (7e-3, 7e-3, 2e-3)),
        "10 mm parallel plates wholly inside a convex epsilon_r=4.2 region "
        "with the nearest air interface 982.5 um from physical copper",
    )
    return (parallel, pcb, material)
