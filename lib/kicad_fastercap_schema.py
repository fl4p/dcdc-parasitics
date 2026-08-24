"""Shared stdlib-only schema helpers for the KiCad FasterCap boundary."""

ZONE_DUMP_FORMAT = "dcdc-fastercap-zones-v1"


def parse_group(value):
    if "=" not in value:
        raise ValueError("group must use NAME=NET[,NET...] syntax")
    name, raw_nets = value.split("=", 1)
    nets = tuple(net for net in raw_nets.split(",") if net)
    if not name or not nets:
        raise ValueError("group must use NAME=NET[,NET...] syntax")
    return name, nets
