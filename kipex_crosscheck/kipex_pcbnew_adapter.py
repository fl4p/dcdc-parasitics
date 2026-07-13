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
import re
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


def board_copper_layers(board):
    """Enabled copper layers, top->bottom. Raises on any layer we cannot map.

    Multilayer support: earlier the adapter forced everything onto F.Cu/B.Cu.
    On real 4-layer power boards the commutation-loop return runs through the
    inner planes, so a 2-layer view opens the loop; we now mesh every mapped
    copper layer (F, In1, In2, B).

    A board with copper beyond In2 is REFUSED, not silently filtered: dropping
    In3/In4 would remove tracks, pour mesh and via landings from the model and
    still print a confident L_loop (too high, or an open loop) with no error.
    Extending support means extending LAYER_MAP *and* the KiPEX BoardLayer enum."""
    cu = list(board.GetEnabledLayers().CuStack())
    unmapped = [l for l in cu if l not in LAYER_MAP]
    if unmapped:
        names = ", ".join(board.GetLayerName(l) for l in unmapped)
        raise NotImplementedError(
            f"board enables copper layers this adapter cannot map: {names}. "
            f"Only F.Cu/In1.Cu/In2.Cu/B.Cu are supported — meshing without the "
            f"others would silently drop conduction paths from the cross-check. "
            f"Extend LAYER_MAP and the KiPEX BoardLayer enum to proceed.")
    return cu


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
        for pcb_layer in board_copper_layers(board):
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


_STACKUP_LAYER_RE = re.compile(
    r'\(layer\s+"([^"]+)"\s*\(type\s+"([^"]+)"\)'
    r'(?:\s*\(color\s+"[^"]*"\))?'
    r'(?:\s*\(thickness\s+([0-9.]+)[^)]*\))?')


class StackupUnparseable(Exception):
    """The board HAS a stackup block, but we could not read it faithfully.

    Distinct from "no stackup block" on purpose: an absent stackup is a known,
    benign state (every Altium-imported board), while an unreadable one means we
    are about to substitute a guess for data that EXISTS. Collapsing both to None
    would make a parse failure indistinguishable from a board that never had a
    stackup — absence of evidence encoding absence of the problem."""


def read_board_stackup(path):
    """Real per-layer thicknesses from the board's `(stackup ...)` block.

    Returns an ordered list of (kind, name, thickness_nm) for the copper and
    dielectric layers, None when the board carries NO stackup block, and raises
    StackupUnparseable when a block is present but cannot be read faithfully.

    pcbnew's GetStackupDescriptor() is not usable from SWIG (it returns an
    opaque SwigPyObject with no GetList), so we read the S-expression."""
    with open(path) as f:
        txt = f.read()
    i = txt.find("(stackup")
    if i < 0:
        return None  # no stackup block at all (every Altium-imported board)
    depth, end = 0, None
    for j in range(i, len(txt)):
        if txt[j] == "(":
            depth += 1
        elif txt[j] == ")":
            depth -= 1
            if depth == 0:
                end = j
                break
    if end is None:
        raise StackupUnparseable("unbalanced parens in the (stackup) block")
    block = txt[i:end]
    # A dielectric with sublayers repeats (thickness ...) inside one (layer ...);
    # our flat regex would take only the first and understate the z span.
    if "addsublayer" in block:
        raise StackupUnparseable("stackup uses sublayered dielectrics (addsublayer)")
    out = []
    for name, typ, thick in _STACKUP_LAYER_RE.findall(block):
        t = typ.lower()
        if t == "copper":
            kind = "copper"
        elif t in ("core", "prepreg") or name.startswith("dielectric"):
            kind = "dielectric"
        else:
            continue  # silk/mask/paste: not part of the copper-to-copper z span
        if not thick:
            raise StackupUnparseable(f"layer {name!r} has no thickness")
        out.append((kind, name, int(round(float(thick) * 1e6))))  # mm -> nm
    if not out:
        raise StackupUnparseable("stackup block lists no copper/dielectric layers")
    return out


class Stackup:
    CU_T = 35_000  # 35 um copper in nm

    def __init__(self, board=None, path=None):
        # All mapped copper layers, top->bottom. Layer z comes from the board's
        # real stackup when it has one; otherwise from equal dielectric gaps
        # summing to the board thickness. Either way each inner plane gets a z,
        # so the via barrels (multi-layer now) and the inner-plane return path
        # are modelled instead of collapsed onto F/B.
        #
        # The gap matters: loop-return current image-couples to the NEAREST
        # plane, so a wrong F<->In1 spacing scales L_loop directly. A typical
        # 4-layer build is asymmetric (thin ~0.2 mm prepreg F<->In1, thick core
        # in the middle); the equal-gap guess puts In1 ~2.5x too far away.
        thick = pcbnew.FromMM(1.6)
        cu_layers = [pcbnew.F_Cu, pcbnew.B_Cu]
        if board is not None:
            thick = board.GetDesignSettings().GetBoardThickness()
            cu_layers = board_copper_layers(board)
        n = len(cu_layers)

        real, unparseable = None, None
        if path:
            try:
                real = read_board_stackup(path)
            except StackupUnparseable as e:
                unparseable = str(e)
                sys.stderr.write(
                    f"WARNING: this board HAS a (stackup) block but it could not be "
                    f"read ({e}) — falling back to a GUESSED uniform layer spacing. "
                    f"The real z data exists and is being ignored; L_loop scales with "
                    f"the F<->In1 gap, so fix the reader before trusting this run.\n")
        if real is not None:
            n_cu = sum(1 for k, _, _ in real if k == "copper")
            if n_cu != n:
                sys.stderr.write(
                    f"WARNING: board stackup lists {n_cu} copper layers but "
                    f"{n} are enabled; ignoring the stackup block and GUESSING "
                    f"uniform layer spacing.\n")
                unparseable = f"copper count {n_cu} != {n} enabled layers"
                real = None

        self.layers = []
        if real is not None:
            self.z_source = "board_stackup"
            cu_i = 0
            for kind, _name, t in real:
                if kind == "copper":
                    self.layers.append(
                        StackLayer(LAYER_MAP[cu_layers[cu_i]], t, "copper"))
                    cu_i += 1
                else:
                    self.layers.append(
                        StackLayer(BoardLayer.BL_UNDEFINED, t, "fr4"))
            # trailing dielectric (below the last copper) is not part of the span
            while self.layers and self.layers[-1].layer == BoardLayer.BL_UNDEFINED:
                self.layers.pop()
        else:
            # No stackup block (e.g. every Altium-imported board), or one we could
            # not read: the inner-plane z is a GUESS. Say so — an inflated F<->In1
            # gap inflates L_loop, and a cross-check that silently guessed its
            # geometry is not a cross-check. The two cases are NOT the same and the
            # provenance string keeps them apart.
            self.z_source = ("uniform_gap_guess_after_parse_failure: " + unparseable
                             if unparseable else "uniform_gap_guess_no_stackup_block")
            diel = max(1, int((thick - n * self.CU_T) / max(1, n - 1)))
            if n > 2:
                sys.stderr.write(
                    f"WARNING: board has no (stackup) block — inner-plane z is a "
                    f"GUESS ({n} copper layers at equal {diel/1e6:.3f} mm gaps over "
                    f"{thick/1e6:.2f} mm). Real 4-layer builds are asymmetric (thin "
                    f"F<->In1 prepreg), and L_loop scales with that gap, so treat "
                    f"the absolute value as provisional.\n")
            for i, l in enumerate(cu_layers):
                self.layers.append(StackLayer(LAYER_MAP[l], self.CU_T, "copper"))
                if i < n - 1:
                    self.layers.append(
                        StackLayer(BoardLayer.BL_UNDEFINED, diel, "fr4"))


class Board:
    def __init__(self, path):
        self._path = path
        self._board = pcbnew.LoadBoard(path)
        board_copper_layers(self._board)  # fail fast on unmappable copper
        self._fps = [FootprintInstance(fp) for fp in self._board.GetFootprints()]

    def get_stackup(self):
        return Stackup(self._board, path=self._path)

    def get_tracks(self):
        # PCB_ARC is copper too. This adapter models a track as a straight
        # start->end segment, which is wrong for an arc, so REFUSE a board that
        # has any rather than silently dropping that copper from the mesh (a
        # dropped arc opens the path it was carrying and biases L_loop high).
        arcs = [t for t in self._board.GetTracks()
                if t.GetClass() == "PCB_ARC" and t.GetLayer() in LAYER_MAP]
        if arcs:
            raise NotImplementedError(
                f"board has {len(arcs)} copper ARC track(s); this adapter models "
                "tracks as straight segments only. Dropping them would silently "
                "remove conduction paths from the cross-check — implement arc "
                "segmentation before running this board.")
        # every enabled copper layer is mappable (checked in __init__), so this
        # filter only excludes genuinely non-copper track layers
        return [Track(t) for t in self._board.GetTracks()
                if t.GetClass() == "PCB_TRACK" and t.GetLayer() in LAYER_MAP]

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
