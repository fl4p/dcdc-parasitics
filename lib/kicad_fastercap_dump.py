#!/usr/bin/env python3
"""Dump exact KiCad filled-zone contours using only pcbnew and the stdlib."""
import argparse
import json
import sys

from kicad_fastercap_schema import ZONE_DUMP_FORMAT, parse_group


def _chain_points(chain, pcbnew):
    return tuple(
        (pcbnew.ToMM(chain.CPoint(index).x), pcbnew.ToMM(chain.CPoint(index).y))
        for index in range(chain.PointCount())
    )


def collect_filled_zone_dump(board, conductor_groups, pcbnew):
    net_to_group = {}
    groups = []
    for group, nets in conductor_groups.items():
        group = str(group)
        if not group or not nets:
            raise ValueError("conductor groups and net sets must be non-empty")
        groups.append(group)
        for net in nets:
            net = str(net)
            if net in net_to_group:
                raise ValueError(f"KiCad net {net!r} belongs to multiple groups")
            net_to_group[net] = group

    records = []
    for zone in board.Zones():
        net = zone.GetNetname()
        if zone.GetIsRuleArea() or net not in net_to_group:
            continue
        group = net_to_group[net]
        for layer_id in zone.GetLayerSet().Seq():
            if not zone.HasFilledPolysForLayer(layer_id):
                continue
            polyset = zone.GetFilledPolysList(layer_id)
            if polyset is None or polyset.OutlineCount() == 0:
                continue
            layer_name = board.GetLayerName(layer_id)
            for outline_index in range(polyset.OutlineCount()):
                records.append({
                    "group": group,
                    "net": net,
                    "layer": layer_name,
                    "shell": _chain_points(polyset.Outline(outline_index), pcbnew),
                    "holes": tuple(
                        _chain_points(polyset.Hole(outline_index, hole_index), pcbnew)
                        for hole_index in range(polyset.HoleCount(outline_index))
                    ),
                })
    return {
        "format": ZONE_DUMP_FORMAT,
        "groups": groups,
        "records": records,
        "kicad_version": pcbnew.GetBuildVersion(),
        "python_executable": sys.executable,
    }


def write_filled_zone_dump(path, board, conductor_groups, pcbnew):
    dump = collect_filled_zone_dump(board, conductor_groups, pcbnew)
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(dump, stream, separators=(",", ":"))
    return dump


def main(argv=None):
    import pcbnew

    parser = argparse.ArgumentParser(
        description="Dump KiCad filled-zone contours for system-Python triangulation"
    )
    parser.add_argument("pcb")
    parser.add_argument("--group", action="append", required=True,
                        help="output conductor and KiCad nets: NAME=NET[,NET...]")
    parser.add_argument("-o", "--output", required=True)
    args = parser.parse_args(argv)
    try:
        groups = dict(parse_group(value) for value in args.group)
    except ValueError as error:
        parser.error(str(error))
    if len(groups) != len(args.group):
        parser.error("conductor group names must be unique")
    board = pcbnew.LoadBoard(args.pcb)
    dump = write_filled_zone_dump(args.output, board, groups, pcbnew)
    print(f"wrote {len(dump['records'])} filled-zone records to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
