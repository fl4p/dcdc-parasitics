#!/usr/bin/env python3
"""Headless KiPEX-style cross-check for Fugu2 using pcbnew as the board backend.

This is intentionally a temp harness: it reuses KiPEX's Translator meshing/export
logic, but supplies a small adapter over pcbnew because KiPEX's normal kipy IPC
path must be launched from KiCad.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import types
from dataclasses import dataclass

import pcbnew

# KiPEX imports api_warning -> wx. The cross-check path never calls it.
wx = types.ModuleType("wx")
wx.App = type("App", (), {"Get": staticmethod(lambda: None)})
wx.MessageBox = lambda *args, **kwargs: None
sys.modules.setdefault("wx", wx)

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kipex_src")
sys.path.insert(0, ROOT)
_pydeps = os.environ.get("KIPEX_PYDEPS", "/private/tmp/kipex-pydeps06")
sys.path.insert(0, _pydeps)

import translator as kx  # noqa: E402
from kipy.proto.board.board_types_pb2 import BoardLayer, PadType, ViaType  # noqa: E402


PCB = "/Users/fab/dev/ee/hw/Fugu2/Fugu2.kicad_pcb"
OUT = "/private/tmp/kipex-run/fugu2_threeleg"
FASTHENRY = "/Users/fab/dev/vendor/FastHenry2/bin/fasthenry"

LAYER_MAP = {
    pcbnew.F_Cu: BoardLayer.BL_F_Cu,
    pcbnew.B_Cu: BoardLayer.BL_B_Cu,
    pcbnew.In1_Cu: BoardLayer.BL_In1_Cu,
    pcbnew.In2_Cu: BoardLayer.BL_In2_Cu,
}


@dataclass
class Vec:
    x: int
    y: int


@dataclass
class Box:
    position: Vec
    size: Vec

    def center(self):
        return Vec(self.position.x + self.size.x // 2, self.position.y + self.size.y // 2)


@dataclass
class PolyNode:
    point: Vec


@dataclass
class PolyLine:
    nodes: list[PolyNode]


@dataclass
class PolyWithHoles:
    outline: PolyLine
    holes: list[PolyLine]

    def bounding_box(self):
        pts = [n.point for n in self.outline.nodes]
        xmin = min(p.x for p in pts)
        xmax = max(p.x for p in pts)
        ymin = min(p.y for p in pts)
        ymax = max(p.y for p in pts)
        return Box(Vec(xmin, ymin), Vec(xmax - xmin, ymax - ymin))


def chain_to_poly(chain):
    nodes = [PolyNode(Vec(chain.CPoint(i).x, chain.CPoint(i).y))
             for i in range(chain.PointCount())]
    return PolyLine(nodes)


def polyset_to_polys(ps):
    out = []
    for i in range(ps.OutlineCount()):
        holes = [chain_to_poly(ps.Hole(i, h)) for h in range(ps.HoleCount(i))]
        out.append(PolyWithHoles(chain_to_poly(ps.Outline(i)), holes))
    return out


class Net:
    def __init__(self, name):
        self.name = name


class Padstack:
    def __init__(self, pad):
        layers = []
        for layer in (pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.In2_Cu, pcbnew.B_Cu):
            if pad.GetLayerSet().Contains(layer):
                layers.append(LAYER_MAP[layer])
                layers.append(LAYER_MAP[layer])
        self.layers = layers
        drill = pad.GetDrillSize()
        self.drill = types.SimpleNamespace(
            diameter=Vec(drill.x, drill.y),
            start_layer=BoardLayer.BL_F_Cu,
            end_layer=BoardLayer.BL_B_Cu,
        )


class Pad:
    def __init__(self, pad, fp=None):
        self._pad = pad
        self._fp = fp
        self.number = pad.GetNumber()
        self.net = Net(pad.GetNetname())
        self.position = Vec(pad.GetPosition().x, pad.GetPosition().y)
        self.padstack = Padstack(pad)
        self.pad_type = PadType.PT_PTH if pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH else PadType.PT_SMD


class Track:
    def __init__(self, track):
        self._track = track
        self.net = Net(track.GetNetname())
        self.layer = LAYER_MAP[track.GetLayer()]
        self.start = Vec(track.GetStart().x, track.GetStart().y)
        self.end = Vec(track.GetEnd().x, track.GetEnd().y)
        self.width = track.GetWidth()

    def length(self):
        return self._track.GetLength()


class Via:
    def __init__(self, via):
        self._via = via
        self.net = Net(via.GetNetname())
        self.type = ViaType.VT_THROUGH
        self.diameter = via.GetWidth()
        self.position = Vec(via.GetPosition().x, via.GetPosition().y)
        self.padstack = types.SimpleNamespace(
            drill=types.SimpleNamespace(
                start_layer=BoardLayer.BL_F_Cu,
                end_layer=BoardLayer.BL_B_Cu,
            )
        )


class Zone:
    def __init__(self, zone, board):
        self._zone = zone
        self.net = Net(zone.GetNetname())
        self.filled_polygons = {}
        for pcb_layer in (pcbnew.F_Cu, pcbnew.B_Cu):
            if not zone.GetLayerSet().Contains(pcb_layer):
                continue
            try:
                ps = zone.GetFilledPolysList(pcb_layer)
            except Exception:
                continue
            self.filled_polygons[LAYER_MAP[pcb_layer]] = polyset_to_polys(ps)


class Field:
    def __init__(self, value):
        self.text = types.SimpleNamespace(value=value)


class FootprintDef:
    def __init__(self, fp):
        self.pads = [Pad(p, fp) for p in fp.Pads()]
        self.shapes = []


class FootprintInstance:
    def __init__(self, fp):
        self._fp = fp
        self.reference_field = Field(fp.GetReference())
        self.definition = FootprintDef(fp)
        self.layer = LAYER_MAP.get(fp.GetLayer(), BoardLayer.BL_F_Cu)
        self.texts_and_fields = []


class StackLayer:
    def __init__(self, layer, thickness, material):
        self.layer = layer
        self.thickness = thickness
        self.material_name = material


class Stackup:
    def __init__(self, board=None):
        # KiPEX only supports 2-layer via modeling. For multi-layer boards,
        # report only F.Cu + B.Cu + dielectric (inner layers are ignored;
        # through-vias still connect F.Cu<->B.Cu). This is a reasonable
        # approximation for commutation loop L on 4-layer boards where the
        # loop current is primarily on outer layers.
        thick = pcbnew.FromMM(1.6)
        if board is not None:
            thick = board.GetDesignSettings().GetBoardThickness()
        self.layers = [
            StackLayer(BoardLayer.BL_F_Cu, 35_000, "copper"),
            StackLayer(BoardLayer.BL_UNDEFINED, int(thick), "fr4"),
            StackLayer(BoardLayer.BL_B_Cu, 35_000, "copper"),
        ]


class Board:
    def __init__(self, path):
        self._board = pcbnew.LoadBoard(path)
        self._fps = [FootprintInstance(fp) for fp in self._board.GetFootprints()]

    def get_stackup(self):
        return Stackup(self._board)

    def get_tracks(self):
        return [Track(t) for t in self._board.GetTracks()
                if t.GetClass() == "PCB_TRACK" and t.GetLayer() in (pcbnew.F_Cu, pcbnew.B_Cu)]

    def get_vias(self):
        return [Via(t) for t in self._board.GetTracks() if t.GetClass() == "PCB_VIA"]

    def get_zones(self):
        return [Zone(z, self._board) for z in self._board.Zones()]

    def get_pads(self):
        return [p for f in self._fps for p in f.definition.pads]

    def get_footprints(self):
        return self._fps

    def get_pad_shapes_as_polygons(self, pad, layer):
        pcb_layer = pcbnew.F_Cu if layer == BoardLayer.BL_F_Cu else pcbnew.B_Cu
        ps = pcbnew.SHAPE_POLY_SET()
        pad._pad.TransformShapeToPolygon(ps, pcb_layer, 0, 64)
        polys = polyset_to_polys(ps)
        return polys[0] if polys else None

    def find_pad(self, ref, number):
        for f in self._fps:
            if f.reference_field.text.value == ref:
                for p in f.definition.pads:
                    if p.number == str(number):
                        return p
        raise KeyError((ref, number))


def run():
    os.makedirs(OUT, exist_ok=True)
    board = Board(PCB)
    tr = kx.Translator(board, {})
    tr.set_frequency_range(39_000, 3_900_000, 1)
    tr.set_quad_limits(3.0, 1.0)

    bcu = BoardLayer.BL_B_Cu
    fcu = BoardLayer.BL_F_Cu
    # Loop orientation: C18+ -> HS drain, HS source -> LS drain, LS source -> C18-.
    ports = [
        ("P_vin", board.find_pad("C18", 1), bcu, board.find_pad("Q1", 2), fcu),
        ("P_sw", board.find_pad("Q1", 3), fcu, board.find_pad("Q2", 2), fcu),
        ("P_gnd", board.find_pad("Q2", 3), fcu, board.find_pad("C18", 2), bcu),
    ]
    for name, a, al, b, bl in ports:
        tr.add_port_from_pads(a, al, b, bl, name)
    err = tr.translate()
    if err:
        raise RuntimeError(err)
    inp = os.path.join(OUT, "kipex_threeleg.inp")
    with open(inp, "w") as f:
        tr.export(f, "KiPEX translator via pcbnew adapter")

    subprocess.run([FASTHENRY, "-p", "diag", "-S", "kipex", os.path.basename(inp)],
                   cwd=OUT, check=True)
    zmat = os.path.join(OUT, "Zckipex.mat")
    freqs = {}
    freq = None
    n = None
    rows = []
    for line in open(zmat):
        if "Impedance matrix for frequency" in line:
            if freq is not None and rows:
                freqs[freq] = rows
            parts = line.replace("=", " ").split()
            freq = float(parts[4])
            n = int(parts[5])
            rows = []
            continue
        if freq is None:
            continue
        vals = line.replace("j", "").split()
        if n and len(vals) >= 2 * n:
            row = [complex(float(vals[2*i]), float(vals[2*i+1])) for i in range(n)]
            rows.append(row)
    if freq is not None and rows:
        freqs[freq] = rows

    out = {"ports": [p[0] for p in ports], "frequencies": {}}
    for f, z in sorted(freqs.items()):
        zloop = sum(z[i][j] for i in range(3) for j in range(3))
        out["frequencies"][str(f)] = {
            "R_loop_ohm": zloop.real,
            "L_loop_H": zloop.imag / (2 * math.pi * f),
            "port_R_ohm": [z[i][i].real for i in range(3)],
            "port_L_H": [z[i][i].imag / (2 * math.pi * f) for i in range(3)],
            "Z_matrix": [[(z[i][j].real, z[i][j].imag) for j in range(3)] for i in range(3)],
        }
    with open(os.path.join(OUT, "summary.json"), "w") as f:
        json.dump(out, f, indent=2)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    run()
