#!/usr/bin/env python3
"""Dump KiCad copper, drills, outline, and completeness census using pcbnew."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

from kicad_palace_schema import (
    PCB_VOLUME_DUMP_FORMAT, check_board_outline_fill, parse_group,
)


MAX_ERROR_MM = 1e-3


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _chain_points(chain, pcbnew):
    return tuple(
        (pcbnew.ToMM(chain.CPoint(index).x), pcbnew.ToMM(chain.CPoint(index).y))
        for index in range(chain.PointCount())
    )


def _polyset_polygons(polyset, pcbnew):
    return tuple({
        "shell": _chain_points(polyset.Outline(index), pcbnew),
        "holes": tuple(
            _chain_points(polyset.Hole(index, hole), pcbnew)
            for hole in range(polyset.HoleCount(index))
        ),
    } for index in range(polyset.OutlineCount()))


def _validate_board_outline(outlines, board, pcbnew):
    box = board.GetBoardEdgesBoundingBox()
    # The board is open here, so the denominator can come from KiCad's own
    # Edge.Cuts bounding box rather than from the outline being judged.
    return check_board_outline_fill(
        outlines,
        pcbnew.ToMM(box.GetWidth()) * pcbnew.ToMM(box.GetHeight()),
    )


def _item_uuid(item):
    return str(item.m_Uuid.AsString())


def _item_group(item, net_to_group, item_to_group):
    uuid = _item_uuid(item)
    return item_to_group.get(uuid, net_to_group.get(str(item.GetNetname())))


def _transform_item(item, layer, pcbnew):
    polygons = pcbnew.SHAPE_POLY_SET()
    item.TransformShapeToPolygon(
        polygons,
        layer,
        0,
        pcbnew.FromMM(MAX_ERROR_MM),
        pcbnew.ERROR_INSIDE,
    )
    return _polyset_polygons(polygons, pcbnew)


def _planar_records(item, source_kind, board, copper_layers, group, pcbnew):
    records = []
    for layer in copper_layers:
        if not item.IsOnLayer(layer):
            continue
        if source_kind == "pad" and not item.FlashLayer(layer):
            continue
        for polygon in _transform_item(item, layer, pcbnew):
            records.append({
                "group": group,
                "net": str(item.GetNetname()),
                "layer": board.GetLayerName(layer),
                "source_kind": source_kind,
                "source_uuid": _item_uuid(item),
                **polygon,
            })
    return records


def _zone_records(zone, board, copper_layers, group, pcbnew):
    records = []
    for layer in copper_layers:
        if not zone.IsOnLayer(layer) or not zone.HasFilledPolysForLayer(layer):
            continue
        polyset = zone.GetFilledPolysList(layer)
        if polyset is None:
            continue
        for polygon in _polyset_polygons(polyset, pcbnew):
            records.append({
                "group": group,
                "net": str(zone.GetNetname()),
                "layer": board.GetLayerName(layer),
                "source_kind": "zone",
                "source_uuid": _item_uuid(zone),
                **polygon,
            })
    return records


def _layer_name(board, layer):
    name = board.GetLayerName(layer)
    if not name:
        raise ValueError(f"KiCad layer {layer} has no name")
    return name


def _drill_record(item, source_kind, board, group, pcbnew):
    if source_kind == "via":
        size = item.GetPrimaryDrillSize()
        start = item.TopLayer()
        end = item.BottomLayer()
        plated = True
        shape = "circle"
    else:
        if not item.HasDrilledHole():
            return None
        size = item.GetPrimaryDrillSize()
        start = item.GetPrimaryDrillStartLayer()
        end = item.GetPrimaryDrillEndLayer()
        plated = item.GetAttribute() != pcbnew.PAD_ATTRIB_NPTH
        shape_value = item.GetPrimaryDrillShape()
        if shape_value == pcbnew.PAD_DRILL_SHAPE_CIRCLE:
            shape = "circle"
        elif shape_value == pcbnew.PAD_DRILL_SHAPE_OBLONG:
            shape = "oblong"
        else:
            shape = "unsupported"
    if size.x <= 0 or size.y <= 0:
        raise ValueError("KiCad drilled item has non-positive drill size")
    position = item.GetPosition()
    unsupported = []
    if shape == "unsupported":
        unsupported.append("undefined_drill_shape")
    if shape == "circle" and size.x != size.y:
        unsupported.append("noncircular_round_drill")
    if (item.IsBackdrilledOrPostMachined(start)
            or item.IsBackdrilledOrPostMachined(end)):
        unsupported.append("backdrilled_or_post_machined")
    if item.GetPrimaryDrillCappedFlag():
        unsupported.append("capped")
    if item.GetPrimaryDrillFilledFlag():
        unsupported.append("filled")
    return {
        "group": group,
        "net": str(item.GetNetname()),
        "source_kind": source_kind,
        "source_uuid": _item_uuid(item),
        "center_mm": (pcbnew.ToMM(position.x), pcbnew.ToMM(position.y)),
        "size_mm": (pcbnew.ToMM(size.x), pcbnew.ToMM(size.y)),
        "shape": shape,
        "start_layer": _layer_name(board, start),
        "end_layer": _layer_name(board, end),
        "plated": plated,
        "unsupported_features": unsupported,
    }


def _graphic_items(board):
    items = list(board.GetDrawings())
    for footprint in board.GetFootprints():
        items.extend(footprint.GraphicalItems())
    return tuple(items)


def _mapping(groups, item_groups):
    net_to_group = {}
    item_to_group = {}
    names = []
    for name, nets in groups.items():
        name = str(name)
        if (not name or name in names
                or (not nets and name not in item_groups)):
            raise ValueError(
                "conductor groups must be unique and bind nets or items"
            )
        names.append(name)
        for net in nets:
            net = str(net)
            if net in net_to_group:
                raise ValueError(f"KiCad net {net!r} belongs to multiple groups")
            net_to_group[net] = name
    for name, uuids in item_groups.items():
        if name not in names:
            raise ValueError(f"item group {name!r} has no matching conductor group")
        for uuid in uuids:
            uuid = str(uuid)
            if uuid in item_to_group:
                raise ValueError(f"KiCad item {uuid!r} belongs to multiple groups")
            item_to_group[uuid] = name
    return tuple(names), net_to_group, item_to_group


def _all_copper_groups(board, pcbnew):
    copper_layers = tuple(board.GetEnabledLayers().CuStack())
    items = [*board.GetTracks(), *board.GetPads(), *board.Zones()]
    relevant = [
        item for item in items
        if not (isinstance(item, pcbnew.ZONE) and item.GetIsRuleArea())
        and any(item.IsOnLayer(layer) for layer in copper_layers)
        and (not isinstance(item, pcbnew.PAD)
             or any(item.FlashLayer(layer) for layer in copper_layers))
    ]
    nets = sorted({str(item.GetNetname()) for item in relevant if item.GetNetname()})
    groups: dict[str, tuple[str, ...]] = {name: (name,) for name in nets}
    isolated = sorted(_item_uuid(item) for item in relevant if not item.GetNetname())
    item_groups = {f"isolated:{uuid}": (uuid,) for uuid in isolated}
    groups.update({name: () for name in item_groups})
    return groups, item_groups


def collect_pcb_volume_dump(
        board, conductor_groups, item_groups, pcbnew, *, grouping_policy="explicit"):
    groups, net_to_group, item_to_group = _mapping(
        conductor_groups, item_groups
    )
    if grouping_policy not in ("explicit", "all_nets_and_isolated_items"):
        raise ValueError("unsupported conductor grouping policy")
    copper_layers = tuple(board.GetEnabledLayers().CuStack())
    if not copper_layers:
        raise ValueError("KiCad board has no enabled copper layers")
    records = []
    drills = []
    census = {
        "included": {name: 0 for name in ("track", "via", "pad", "zone")},
        "unassigned": [],
        "unsupported": [],
    }

    for item in board.GetTracks():
        source_kind = "via" if isinstance(item, pcbnew.PCB_VIA) else "track"
        group = _item_group(item, net_to_group, item_to_group)
        if group is None:
            census["unassigned"].append({
                "source_kind": source_kind,
                "source_uuid": _item_uuid(item),
                "net": str(item.GetNetname()),
            })
            continue
        records.extend(_planar_records(
            item, source_kind, board, copper_layers, group, pcbnew
        ))
        if source_kind == "via":
            drills.append(_drill_record(item, source_kind, board, group, pcbnew))
        census["included"][source_kind] += 1

    for pad in board.GetPads():
        if not any(pad.IsOnLayer(layer) for layer in copper_layers):
            continue
        if not any(pad.FlashLayer(layer) for layer in copper_layers):
            drill = _drill_record(pad, "pad", board, None, pcbnew)
            if drill is not None:
                if drill["plated"]:
                    census["unsupported"].append({
                        "source_kind": "pad",
                        "source_uuid": _item_uuid(pad),
                        "features": ["plated_drill_without_copper_flash"],
                    })
                drills.append(drill)
            census["included"]["pad"] += 1
            continue
        group = _item_group(pad, net_to_group, item_to_group)
        if group is None:
            census["unassigned"].append({
                "source_kind": "pad",
                "source_uuid": _item_uuid(pad),
                "net": str(pad.GetNetname()),
            })
            continue
        records.extend(_planar_records(
            pad, "pad", board, copper_layers, group, pcbnew
        ))
        drill = _drill_record(pad, "pad", board, group, pcbnew)
        if drill is not None:
            drills.append(drill)
        census["included"]["pad"] += 1

    for zone in board.Zones():
        if zone.GetIsRuleArea() or not any(
                zone.IsOnLayer(layer) for layer in copper_layers):
            continue
        group = _item_group(zone, net_to_group, item_to_group)
        if group is None:
            census["unassigned"].append({
                "source_kind": "zone",
                "source_uuid": _item_uuid(zone),
                "net": str(zone.GetNetname()),
            })
            continue
        records.extend(_zone_records(
            zone, board, copper_layers, group, pcbnew
        ))
        census["included"]["zone"] += 1

    copper_layer_set = set(copper_layers)
    for item in _graphic_items(board):
        layers = set(item.GetLayerSet().Seq())
        if layers & copper_layer_set:
            census["unsupported"].append({
                "source_kind": "copper_graphic",
                "source_uuid": _item_uuid(item),
                "layers": sorted(
                    board.GetLayerName(layer) for layer in layers & copper_layer_set
                ),
            })
    for drill in drills:
        if drill["unsupported_features"]:
            census["unsupported"].append({
                "source_kind": drill["source_kind"],
                "source_uuid": drill["source_uuid"],
                "features": drill["unsupported_features"],
            })

    outline = pcbnew.SHAPE_POLY_SET()
    # ConvertBrdLayerToPolygonalContours, which this used to call, renders the
    # Edge.Cuts *graphics* by stroking each one at its line width. A board
    # outlined with four 0.1 mm gr_lines therefore came back as four capsules,
    # which the consumer unioned into a 50 um picture frame: the FR4 core and
    # both masks covered 0.47% of the board and the whole interior solved as
    # air. GetBoardPolygonOutlines chains the segments into closed loops and
    # returns the region they enclose, which is the board. Infer is False so an
    # unclosed outline raises here rather than being silently replaced by the
    # Edge.Cuts bounding box.
    if not board.GetBoardPolygonOutlines(outline, False):
        raise ValueError("KiCad board has no closed Edge.Cuts outline")
    outlines = _polyset_polygons(outline, pcbnew)
    _validate_board_outline(outlines, board, pcbnew)
    records.sort(key=lambda item: (
        item["group"], item["layer"], item["source_kind"],
        item["source_uuid"], item["shell"],
    ))
    drills.sort(key=lambda item: (item["source_uuid"], item["source_kind"]))
    census["unassigned"].sort(key=lambda item: (
        item["source_kind"], item["source_uuid"]
    ))
    census["unsupported"].sort(key=lambda item: (
        item["source_kind"], item["source_uuid"]
    ))
    return {
        "format": PCB_VOLUME_DUMP_FORMAT,
        "groups": groups,
        "grouping_policy": grouping_policy,
        "copper_layers": tuple(board.GetLayerName(layer) for layer in copper_layers),
        "records": records,
        "drills": drills,
        "board_outlines": outlines,
        "census": census,
        "polygon_error_mm": MAX_ERROR_MM,
        "kicad_version": pcbnew.GetBuildVersion(),
        "python_executable": sys.executable,
    }


def write_pcb_volume_dump(
        path, board, conductor_groups, item_groups, pcbnew, *, source_pcb_path,
        grouping_policy="explicit"):
    dump = collect_pcb_volume_dump(
        board, conductor_groups, item_groups, pcbnew,
        grouping_policy=grouping_policy,
    )
    source_pcb_path = Path(source_pcb_path).resolve()
    dump["source_pcb_path"] = str(source_pcb_path)
    dump["source_pcb_sha256"] = _file_sha256(source_pcb_path)
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(dump, stream, separators=(",", ":"))
    return dump


def main(argv=None):
    import pcbnew

    parser = argparse.ArgumentParser(
        description="Dump complete grouped KiCad PCB volumes for Palace"
    )
    parser.add_argument("pcb")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--group", action="append",
                           help="conductor and KiCad nets: NAME=NET[,NET...]")
    selection.add_argument(
        "--all-copper", action="store_true",
        help="preserve every named net and no-net item as separate conductors",
    )
    parser.add_argument("--item-group", action="append", default=[],
                        help="conductor and KiCad item UUIDs: NAME=UUID[,UUID...]")
    parser.add_argument("-o", "--output", required=True)
    args = parser.parse_args(argv)
    board = pcbnew.LoadBoard(args.pcb)
    if args.all_copper:
        if args.item_group:
            parser.error("--item-group cannot be combined with --all-copper")
        groups, item_groups = _all_copper_groups(board, pcbnew)
        grouping_policy = "all_nets_and_isolated_items"
    else:
        try:
            groups = dict(parse_group(value) for value in args.group)
            item_groups = dict(parse_group(value) for value in args.item_group)
        except ValueError as error:
            parser.error(str(error))
        if (len(groups) != len(args.group)
                or len(item_groups) != len(args.item_group)):
            parser.error("group names must be unique within each option")
        grouping_policy = "explicit"
    dump = write_pcb_volume_dump(
        args.output, board, groups, item_groups, pcbnew,
        source_pcb_path=args.pcb,
        grouping_policy=grouping_policy,
    )
    print(
        f"wrote {len(dump['records'])} copper polygons, "
        f"{len(dump['drills'])} drills to {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
