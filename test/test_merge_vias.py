#!/usr/bin/env python3
"""Unit tests for the --merge-vias feature (pure-python layers).

Covers _cluster_vias (clique bound + order independence), _via_in_pour (pour
gating), and add_vias behaviour (gate/off-pour vias stay per-via, pour-embedded
power vias merge with w=sum(d) at the centroid, singletons unmoved, provenance
keys stable, off = legacy). extract_parasitics YAML/CLI forwarding lives at the
bottom. Plain asserts, no framework, matching the repo style.
"""
import itertools
import os
import sys
import types

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

# kicad_geom does `import pcbnew` at module top; stub it so we can import under
# system python, then give it the one constant add_vias reads.
sys.modules.setdefault("pcbnew", types.ModuleType("pcbnew"))
import kicad_geom  # noqa: E402

_VIA_T = object()
kicad_geom.pcbnew.PCB_VIA_T = _VIA_T
NM = kicad_geom.NM

# 2-layer stack so each barrel is exactly ONE vertical seg (span [F.Cu, B.Cu]) —
# makes "barrel count == seg count" and width assertions trivial.
_CU = [0, 2]
_ZMAP = {0: 0.0, 2: -0.5}


class _Pos:
    def __init__(self, x, y):
        self.x, self.y = x, y


class _Via:
    def __init__(self, net, x_mm, y_mm, d_mm=0.3, top=0, bot=2):
        self._net, self._d, self._top, self._bot = net, d_mm * NM, top, bot
        self._x, self._y = x_mm * NM, y_mm * NM

    def Type(self):
        return _VIA_T

    def GetNetname(self):
        return self._net

    def GetPosition(self):
        return _Pos(self._x, self._y)

    def GetWidth(self):
        return self._d

    def TopLayer(self):
        return self._top

    def BottomLayer(self):
        return self._bot


class _LayerSet:
    def CuStack(self):
        return list(_CU)


class _Board:
    def __init__(self, vias):
        self._vias = vias

    def GetTracks(self):
        return self._vias

    def GetEnabledLayers(self):
        return _LayerSet()


class _Model:
    """Captures node/seg emission + carries zone_nodes/meta/_pos like the real
    Model, so add_vias' reachability gate can be exercised."""
    def __init__(self):
        self.segs = []          # (na, nb, width)
        self._nodes = {}
        self.zone_nodes = set()
        self.meta = {}          # node name -> (net, lid)
        self._pos = {}          # node name -> (x, y, z)
        self.via_merge = None
        self._i = 0

    def node(self, net, lid, x, y, z):
        key = (net, lid, round(x, 4), round(y, 4))
        if key not in self._nodes:
            nm = f"N{self._i}"
            self._i += 1
            self._nodes[key] = (nm, x, y)
            self.meta[nm] = (net, lid)
            self._pos[nm] = (x, y, z)
        return self._nodes[key][0]

    def add_zone_node(self, net, lid, x, y):
        self.zone_nodes.add(self.node(net, lid, x, y, 0.0))

    def seg(self, na, nb, w, h=None):
        self.segs.append((na, nb, round(w, 4)))

    def node_xy(self, name):
        for nm, x, y in self._nodes.values():
            if nm == name:
                return (round(x, 4), round(y, 4))
        return None


def _pour_left_of(x_max):
    """A fill predicate: 'inside pour' iff x < x_max. Same fn for all (net,lid)
    keys we register."""
    return lambda x, y: x < x_max


def _pour_index(nets_layers, x_max=15.0):
    fn = _pour_left_of(x_max)
    return {(net, lid): fn for (net, lid) in nets_layers}


POWER = {"Vb", "GND", "SW"}
_PI = _pour_index([(n, l) for n in POWER for l in _CU])  # pour on both layers, x<15


# ---------- _cluster_vias: clique bound + order independence ----------

def _v(x, y):
    return dict(net="Vb", x=x, y=y, d=0.3, top=0, bot=2)


def test_cluster_pairwise_bound_holds_for_every_order():
    # 0.0 / 0.9 / 1.8 mm, r=1.0: 0.0 and 1.8 are 1.8mm apart and must NEVER co-merge
    for order in itertools.permutations([0.0, 0.9, 1.8]):
        clusters = kicad_geom._cluster_vias([_v(x, 0.0) for x in order], 1.0 ** 2)
        for cl in clusters:
            xs = [m["x"] for m in cl]
            assert max(xs) - min(xs) <= 1.0 + 1e-9, (order, xs)


def test_cluster_tight_field_merges_to_one():
    field = [_v(10, 10), _v(10.3, 10), _v(10, 10.3), _v(10.3, 10.3)]
    clusters = kicad_geom._cluster_vias(field, 1.0 ** 2)
    assert len(clusters) == 1 and len(clusters[0]) == 4


# ---------- _via_in_pour ----------

def test_via_in_pour_requires_both_endpoint_layers():
    assert kicad_geom._via_in_pour(_PI, "Vb", 5.0, 5.0, 0, 2) is True
    # off-pour location (x >= 15)
    assert kicad_geom._via_in_pour(_PI, "Vb", 20.0, 5.0, 0, 2) is False
    # net with no pour entry
    assert kicad_geom._via_in_pour(_PI, "HG", 5.0, 5.0, 0, 2) is False
    # missing pour on one endpoint layer -> not eligible
    pi_top_only = {("Vb", 0): _pour_left_of(15.0)}
    assert kicad_geom._via_in_pour(pi_top_only, "Vb", 5.0, 5.0, 0, 2) is False


# ---------- add_vias behaviour ----------

def _run(vias, merge_vias=True, radius=1.0, pour_index=_PI, roi=None,
         pitch=None, zone_nodes=None):
    m = _Model()
    for (net, lid, x, y) in (zone_nodes or []):
        m.add_zone_node(net, lid, x, y)
    kicad_geom.add_vias(_Board(vias), m, _ZMAP, POWER | {"HG"},
                        merge_vias=merge_vias, merge_radius=radius,
                        merge_nets=POWER, pour_index=pour_index, roi=roi, pitch=pitch)
    return m


def test_off_disables_merge_and_leaves_provenance_none():
    m = _run([_Via("Vb", 10, 10), _Via("Vb", 10.2, 10)], merge_vias=False)
    assert m.via_merge is None
    assert len(m.segs) == 2           # per-via, unmerged


def test_gate_vias_never_merge():
    # two coincident-ish gate vias — dense, but gate nets must stay per-via
    m = _run([_Via("HG", 10, 10), _Via("HG", 10.1, 10)])
    assert len(m.segs) == 2
    assert m.via_merge["powernet_vias"] == 0
    assert m.via_merge["clusters_merged"] == 0
    assert m.via_merge["barrels_after"] == 2     # per-via gate barrels ARE counted


def test_pour_embedded_power_vias_merge_with_summed_width_at_centroid():
    vias = [_Via("Vb", 10.0, 10.0, 0.3), _Via("Vb", 10.2, 10.0, 0.3),
            _Via("Vb", 10.0, 10.2, 0.3)]
    m = _run(vias)
    assert len(m.segs) == 1                       # 3 vias -> 1 barrel
    assert m.segs[0][2] == round(0.9, 4)          # width = sum of diameters
    # centroid location
    cx, cy = round((10.0 + 10.2 + 10.0) / 3, 4), round((10.0 + 10.0 + 10.2) / 3, 4)
    assert m.node_xy(m.segs[0][0]) == (cx, cy)
    assert m.via_merge["clusters_merged"] == 1
    assert m.via_merge["vias_eligible"] == 3
    assert m.via_merge["barrels_after"] == 1


def test_off_pour_power_via_stays_per_via_at_original_coords():
    # x=20 is off-pour (>=15); must not merge and must not move
    m = _run([_Via("Vb", 20.0, 5.0, 0.3), _Via("Vb", 20.1, 5.0, 0.3)])
    assert len(m.segs) == 2
    assert m.via_merge["excluded_off_pour_or_roi"] == 2
    assert m.via_merge["vias_eligible"] == 0
    assert m.via_merge["clusters_merged"] == 0
    assert m.via_merge["barrels_after"] == 2     # off-pour per-via barrels counted
    assert m.node_xy(m.segs[0][0]) == (20.0, 5.0)


def test_merge_falls_back_when_centroid_not_near_mesh_node():
    """Polygon+ROI containment is necessary but NOT sufficient: if a coarse pitch
    left no pour-mesh node within 3*pitch of the centroid, the merged barrel would
    float and be pruned. With pitch given (add_zones already ran), the cluster must
    fall back to per-via unless a mesh node is actually reachable."""
    vias = [_Via("Vb", 10.0, 10.0, 0.3), _Via("Vb", 10.2, 10.0, 0.3)]
    # only pour-mesh node is far away (> 3*0.5 = 1.5 mm from centroid ~10.1,10.0)
    far = _run(vias, pitch=0.5, zone_nodes=[("Vb", 0, 50.0, 50.0), ("Vb", 2, 50.0, 50.0)])
    assert len(far.segs) == 2                              # per-via fallback
    assert far.via_merge["clusters_merged"] == 0
    assert far.via_merge["unreachable_fallback_clusters"] == 1
    # a mesh node NEAR the centroid -> merge proceeds
    near = _run(vias, pitch=0.5, zone_nodes=[("Vb", 0, 10.1, 10.0), ("Vb", 2, 10.1, 10.0)])
    assert near.via_merge["clusters_merged"] == 1
    assert near.via_merge["unreachable_fallback_clusters"] == 0


def test_merge_requires_reachability_on_every_pour_layer():
    """A merged barrel must bond on EVERY spanned layer that carries same-net pour.
    Reaching the top pour but not the bottom pour (which the individual vias could
    bond) must still fall back — 'any layer' reachability is insufficient."""
    vias = [_Via("Vb", 10.0, 10.0), _Via("Vb", 10.2, 10.0)]  # span [0,2], centroid ~10.1
    # top (lid 0) node near centroid; bottom (lid 2) node only far away
    m = _run(vias, pitch=0.5, zone_nodes=[("Vb", 0, 10.1, 10.0), ("Vb", 2, 50.0, 50.0)])
    assert m.via_merge["clusters_merged"] == 0
    assert m.via_merge["unreachable_fallback_clusters"] == 1
    assert len(m.segs) == 2


def test_merge_reachability_boundary_matches_stitch_strict_lt():
    """The gate must use the SAME strict < as stitch_zones. A centroid exactly
    3*pitch from the only mesh node is NOT bonded by stitch_zones, so the gate must
    treat it as unreachable — an inclusive <= would merge a barrel that then floats
    and is silently pruned."""
    vias = [_Via("Vb", 2.9, 0.0), _Via("Vb", 3.1, 0.0)]   # centroid (3.0, 0.0)
    # mesh nodes exactly 3*pitch away: dist^2 = 9.0 == (3*1.0)^2  -> strict < fails
    at = _run(vias, pitch=1.0, zone_nodes=[("Vb", 0, 0.0, 0.0), ("Vb", 2, 0.0, 0.0)])
    assert at.via_merge["clusters_merged"] == 0
    assert at.via_merge["unreachable_fallback_clusters"] == 1
    # a hair inside 3*pitch -> bonds -> merges
    inside = _run(vias, pitch=1.0, zone_nodes=[("Vb", 0, 0.05, 0.0), ("Vb", 2, 0.05, 0.0)])
    assert inside.via_merge["clusters_merged"] == 1


def test_merge_falls_back_when_pour_layer_has_no_mesh_nodes():
    """pour_index says the bottom endpoint layer is filled at the centroid, but
    add_zones created ZERO mesh nodes there. A missing bucket must count as
    UNREACHABLE (the barrel can't bond that pour), not be skipped as 'no pour'."""
    vias = [_Via("Vb", 10.0, 10.0), _Via("Vb", 10.2, 10.0)]  # span [0,2], _PI pours both
    # only a TOP (lid 0) mesh node near the centroid; NO bottom (lid 2) nodes at all
    m = _run(vias, pitch=0.5, zone_nodes=[("Vb", 0, 10.1, 10.0)])
    assert m.via_merge["clusters_merged"] == 0
    assert m.via_merge["unreachable_fallback_clusters"] == 1
    assert len(m.segs) == 2


def test_in_pour_but_outside_roi_stays_per_via():
    # both vias are inside the full-board pour (x<15) and within merge_radius, but
    # the meshed ROI excludes them -> no zone nodes to bond to -> must NOT merge.
    roi = (0.0, 0.0, 8.0, 8.0)   # ROI is x,y in [0,8]; vias at x=10 are outside
    m = _run([_Via("Vb", 10.0, 10.0, 0.3), _Via("Vb", 10.2, 10.0, 0.3)], roi=roi)
    assert len(m.segs) == 2                       # per-via, not merged
    assert m.via_merge["vias_eligible"] == 0
    assert m.via_merge["excluded_off_pour_or_roi"] == 2
    assert m.via_merge["clusters_merged"] == 0
    assert m.node_xy(m.segs[0][0]) == (10.0, 10.0)  # not moved to a centroid
    # same vias merge once the ROI includes them
    m2 = _run([_Via("Vb", 10.0, 10.0, 0.3), _Via("Vb", 10.2, 10.0, 0.3)],
              roi=(0.0, 0.0, 14.0, 14.0))
    assert m2.via_merge["clusters_merged"] == 1


def test_singleton_pour_via_kept_at_original_coords():
    m = _run([_Via("Vb", 5.0, 5.0, 0.3)])
    assert len(m.segs) == 1
    assert m.segs[0][2] == round(0.3, 4)
    assert m.node_xy(m.segs[0][0]) == (5.0, 5.0)
    assert m.via_merge["clusters_merged"] == 0


def test_provenance_keys_are_stable():
    m = _run([_Via("Vb", 10.0, 10.0), _Via("Vb", 10.2, 10.0)])
    assert set(m.via_merge) == {
        "enabled", "radius_mm", "powernet_vias", "vias_eligible",
        "excluded_off_pour_or_roi", "barrels_after", "clusters_merged",
        "centroid_fallback_clusters", "unreachable_fallback_clusters",
        "max_cluster_extent_mm",
    }
    assert m.via_merge["enabled"] is True
    assert m.via_merge["radius_mm"] == 1.0


# ---------- extract_parasitics YAML/CLI forwarding ----------

def _import_extract():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, root)
    import extract_parasitics  # noqa
    return extract_parasitics


def test_extract_forwards_merge_vias_cli_over_yaml():
    ep = _import_extract()
    import tempfile
    fd, cfg = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w") as fh:
        fh.write("pcb: /b.kicad_pcb\nsw: SW\ngnd: GND\nout: o\nmerge_vias: true\n"
                 "merge_via_radius: 2.0\n")
    # YAML enables it
    a = ep.parse_args(["--config", cfg])
    assert a.merge_vias is True and a.merge_via_radius == 2.0
    # CLI --no-merge-vias overrides YAML true
    a = ep.parse_args(["--config", cfg, "--no-merge-vias"])
    assert a.merge_vias is False
    # default off when unspecified
    a = ep.parse_args(["/b.kicad_pcb", "--sw", "SW", "--gnd", "GND", "-o", "o"])
    assert a.merge_vias is False and a.merge_via_radius == 1.0


def test_run_geom_appends_flags_only_when_enabled():
    ep = _import_extract()
    from types import SimpleNamespace
    base = dict(pcb="b.kicad_pcb", sw="SW", gnd="GND", cin_parallel=1, lead_mm=0.1,
                nwinc=1, nhinc=1, cu_temp=20.0, cu_thickness=0.035, lf_freq=1e5,
                weld_tol=0.6, zone_mesh="grid", terminal_mode="padland", margin=8.0,
                vin=None, hs_gate=None, ls_gate=None, hs_ref=None, ls_ref=None,
                cin_refs=None, cin_loop_refs=None, cin_network_refs=None,
                hs_kelvin=False, ls_kelvin=False, include_bulk_cin=False,
                emit_cin_network=False, cin_network_model="scalar_trunk",
                cin_extraction_basis="full_loop", cin_closure="cell_bridge",
                parallel_fets="lumped", allow_missing_gate_ports=False)
    captured = {}

    def fake_run(cmd, capture_output, text, env=None):
        captured["cmd"] = cmd
        # write a minimal sidecar so run_geom's json.load + require_gate_ports pass
        inp = cmd[cmd.index("-o") + 1]
        import json
        with open(inp + ".ports.json", "w") as f:
            json.dump({"ports": ["P_pwr"], "topo": {}}, f)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    import tempfile
    d = tempfile.mkdtemp()
    orig_run = ep.subprocess.run
    orig_req = ep.require_gate_ports
    ep.subprocess.run = fake_run
    ep.require_gate_ports = lambda side, pitch, **kw: None
    try:
        ep.run_geom(SimpleNamespace(merge_vias=True, merge_via_radius=1.5, **base),
                    1.0, d)
        assert "--merge-vias" in captured["cmd"]
        assert "--merge-via-radius" in captured["cmd"]
        assert captured["cmd"][captured["cmd"].index("--merge-via-radius") + 1] == "1.5"
        ep.run_geom(SimpleNamespace(merge_vias=False, merge_via_radius=1.5, **base),
                    1.0, d)
        assert "--merge-vias" not in captured["cmd"]
    finally:
        ep.subprocess.run = orig_run
        ep.require_gate_ports = orig_req


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all merge-vias tests passed")
