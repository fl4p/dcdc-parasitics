"""Shared stdlib-only schema helpers for the KiCad Palace boundary."""

from kicad_fastercap_schema import parse_group


PCB_VOLUME_DUMP_FORMAT = "dcdc-kicad-palace-volumes-v1"


__all__ = ["PCB_VOLUME_DUMP_FORMAT", "parse_group"]
