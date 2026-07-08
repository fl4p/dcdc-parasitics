#!/usr/bin/env python3
"""Unit tests for extract_parasitics.py config parsing.

Plain asserts, no framework, matching the rest of parasitics/test.
"""
import os
import sys
import tempfile
from contextlib import redirect_stderr
from io import StringIO
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import extract_parasitics  # noqa: E402
sys.path.insert(0, os.path.join(ROOT, "lib"))
import pcb_source  # noqa: E402


def _yaml(text):
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    return path


def _expect_exit(fn):
    try:
        with redirect_stderr(StringIO()):
            fn()
    except SystemExit as e:
        return e
    raise AssertionError("expected SystemExit")


def test_yaml_only_supplies_required_args():
    cfg = _yaml("""
pcb: /boards/Fugu2.kicad_pcb
sw: SW
gnd: BuckGND
out: out-par
pitch: [2.0, 1.0]
hs_ref: [Q1, Q3]
ls_ref: [Q2]
cin_loop_refs: [C18, C17, C9]
cin_network_refs: [C18, C17, C9, C27]
emit_cin_network: true
parallel_fets: per-device
weld_tol: 0.7
terminal_mode: finite
margin: 6.5
cu_thickness: 0.07
cin_network_model: matrix
cin_extraction_basis: cap_only
cin_closure: per_fet
allow_scalar_cin: true
allow_missing_gate_ports: true
""")
    args = extract_parasitics.parse_args(["--config", cfg])
    assert args.pcb == "/boards/Fugu2.kicad_pcb"
    assert args.sw == "SW"
    assert args.gnd == "BuckGND"
    assert args.out == "out-par"
    assert args.pitch == [2.0, 1.0]
    assert args.hs_ref == ["Q1", "Q3"]
    assert args.ls_ref == ["Q2"]
    assert args.cin_loop_refs == ["C18", "C17", "C9"]
    assert args.cin_network_refs == ["C18", "C17", "C9", "C27"]
    assert args.emit_cin_network is True
    assert args.parallel_fets == "per-device"
    assert args.weld_tol == 0.7
    assert args.terminal_mode == "finite"
    assert args.margin == 6.5
    assert args.cu_thickness == 0.07
    assert args.cin_network_model == "matrix"
    assert args.cin_extraction_basis == "cap_only"
    assert args.cin_closure == "per_fet"
    assert args.allow_scalar_cin is True
    assert args.allow_missing_gate_ports is True


def test_cli_overrides_yaml_scalars_lists_and_booleans():
    cfg = _yaml("""
pcb: /boards/old.kicad_pcb
sw: OLD_SW
gnd: OLD_GND
out: old-out
pitch: [3.0]
hs_ref: [Q9]
svg: true
cu_thickness: 0.035
""")
    args = extract_parasitics.parse_args([
        "--config", cfg,
        "/boards/new.kicad_pcb",
        "--sw", "SW",
        "--gnd", "GND",
        "--pitch", "2.0", "1.0",
        "--hs-ref", "Q1", "Q3",
        "--no-svg",
        "--cu-thickness", "0.07",
        "-o", "new-out",
    ])
    assert args.pcb == "/boards/new.kicad_pcb"
    assert args.sw == "SW"
    assert args.gnd == "GND"
    assert args.out == "new-out"
    assert args.pitch == [2.0, 1.0]
    assert args.hs_ref == ["Q1", "Q3"]
    assert args.svg is False
    assert args.cu_thickness == 0.07


def test_cin_refs_alias_normalizes_to_cin_loop_refs():
    cfg = _yaml("""
pcb: /boards/Fugu2.kicad_pcb
sw: SW
gnd: BuckGND
out: out-par
cin_refs: [C18, C17]
""")
    args = extract_parasitics.parse_args(["--config", cfg])
    assert args.cin_refs is None
    assert args.cin_loop_refs == ["C18", "C17"]


def test_cin_refs_and_cin_loop_refs_are_mutually_exclusive():
    cfg = _yaml("""
pcb: /boards/Fugu2.kicad_pcb
sw: SW
gnd: BuckGND
out: out-par
cin_refs: [C18]
cin_loop_refs: [C17]
    """)
    e = _expect_exit(lambda: extract_parasitics.parse_args(["--config", cfg]))
    assert e.code == 2


def test_mesh_complexity_line_formats_sidecar_stats():
    side = {"mesh": dict(nodes=10, segs=20, filaments_est=40,
                         nwinc=2, nhinc=1, ports=5, freq_points=11,
                         work_units=8.8e5)}
    line = extract_parasitics.mesh_complexity_line(1.0, side)
    assert "pitch  1.0 mm mesh:" in line
    assert "10 nodes, 20 segs" in line
    assert "~40 filaments" in line
    assert "5 ports, 11 freqs" in line
    assert "work~880k" in line


def test_github_blob_url_normalizes_to_raw():
    url = "https://github.com/org/repo/blob/main/hw/Fugu2/Fugu2.kicad_pcb"
    got = pcb_source.normalize_pcb_url(url)
    assert got == "https://github.com/org/repo/raw/main/hw/Fugu2/Fugu2.kicad_pcb"


def test_resolve_pcb_path_downloads_url_to_workdir():
    calls = []
    def fake_download(url, path):
        calls.append((url, path))
        with open(path, "wb") as fh:
            fh.write(b"(kicad_pcb)")

    workdir = tempfile.mkdtemp()
    got = pcb_source.resolve_pcb_path(
        "https://github.com/org/repo/blob/main/hw/board.kicad_pcb",
        workdir,
        downloader=fake_download)

    assert got == os.path.join(workdir, "board.kicad_pcb")
    assert os.path.exists(got)
    assert calls == [("https://github.com/org/repo/raw/main/hw/board.kicad_pcb", got)]


def _make_board(content=b"(kicad_pcb)", name="board.kicad_pcb"):
    """Write a fake PCB file under a fresh temp dir; return its path."""
    d = tempfile.mkdtemp()
    p = os.path.join(d, name)
    with open(p, "wb") as fh:
        fh.write(content)
    return p


def test_resolve_pcb_path_config_relative_fallback():
    """When cwd-relative resolution misses but config-relative hits, return the
    config-relative path."""
    cfg_dir = tempfile.mkdtemp()
    sub = os.path.join(cfg_dir, "boards")
    os.makedirs(sub)
    board = os.path.join(sub, "Fugu2.kicad_pcb")
    with open(board, "wb") as fh:
        fh.write(b"(kicad_pcb)")
    cfg = os.path.join(cfg_dir, "fugu2.yaml")
    with open(cfg, "w") as fh:
        fh.write("pcb: boards/Fugu2.kicad_pcb\n")
    # run from an unrelated cwd so cwd-relative resolution fails
    cwd = os.getcwd()
    try:
        os.chdir(tempfile.mkdtemp())
        got = pcb_source.resolve_pcb_path(
            "boards/Fugu2.kicad_pcb", "/tmp/wd", config_path=cfg)
    finally:
        os.chdir(cwd)
    assert got == board
    assert os.path.isfile(got)


def test_resolve_pcb_path_config_relative_when_cwd_also_exists_same_content():
    """Both resolve and content matches -> return cwd-relative path (no hard-fail)."""
    content = b"(kicad_pcb identical)"
    cwd_board = _make_board(content, "board.kicad_pcb")
    cfg_dir = tempfile.mkdtemp()
    cfg_board = os.path.join(cfg_dir, "board.kicad_pcb")
    with open(cfg_board, "wb") as fh:
        fh.write(content)
    cfg = os.path.join(cfg_dir, "fugu2.yaml")
    with open(cfg, "w") as fh:
        fh.write("pcb: board.kicad_pcb\n")
    cwd = os.getcwd()
    try:
        os.chdir(os.path.dirname(cwd_board))
        got = pcb_source.resolve_pcb_path(
            "board.kicad_pcb", "/tmp/wd", config_path=cfg)
        # macOS temp dirs sit under /var -> /private/var symlink, so compare
        # via realpath rather than the literal string.
        assert os.path.realpath(got) == os.path.realpath(cwd_board)
        assert os.path.isfile(got)
    finally:
        os.chdir(cwd)


def test_resolve_pcb_path_both_exist_different_content_hard_fails():
    """Both resolve but to different boards (SHA-256 mismatch) -> SystemExit."""
    cwd_board = _make_board(b"(board A)", "board.kicad_pcb")
    cfg_dir = tempfile.mkdtemp()
    cfg_board = os.path.join(cfg_dir, "board.kicad_pcb")
    with open(cfg_board, "wb") as fh:
        fh.write(b"(board B different)")
    cfg = os.path.join(cfg_dir, "fugu2.yaml")
    with open(cfg, "w") as fh:
        fh.write("pcb: board.kicad_pcb\n")
    cwd = os.getcwd()
    try:
        os.chdir(os.path.dirname(cwd_board))
        e = _expect_exit(lambda: pcb_source.resolve_pcb_path(
            "board.kicad_pcb", "/tmp/wd", config_path=cfg))
    finally:
        os.chdir(cwd)
    msg = str(e.code)
    assert "two different boards" in msg
    assert "SHA-256" in msg


def test_resolve_pcb_path_absolute_ignores_config_path():
    """An absolute pcb path is returned as-is regardless of config_path."""
    board = _make_board(b"(kicad_pcb)")
    cfg = _yaml("pcb:ignored-because-absolute\nsw: SW\ngnd: GND\nout: out\n")
    got = pcb_source.resolve_pcb_path(board, "/tmp/wd", config_path=cfg)
    assert got == board


def test_resolve_pcb_path_no_config_falls_back_to_cwd_relative():
    """Without config_path, behave like the legacy cwd-relative passthrough."""
    board = _make_board(b"(kicad_pcb)")
    cwd = os.getcwd()
    try:
        os.chdir(os.path.dirname(board))
        got = pcb_source.resolve_pcb_path("board.kicad_pcb", "/tmp/wd")
        assert os.path.realpath(got) == os.path.realpath(board)
        assert os.path.isfile(got)
    finally:
        os.chdir(cwd)


def test_resolve_pcb_path_neither_exists_returns_cwd_path():
    """Neither cwd- nor config-relative exists -> return cwd path (let downstream
    open() raise FileNotFoundError with the cwd-relative path)."""
    cfg_dir = tempfile.mkdtemp()
    cfg = os.path.join(cfg_dir, "fugu2.yaml")
    with open(cfg, "w") as fh:
        fh.write("pcb: missing/board.kicad_pcb\n")
    cwd = os.getcwd()
    try:
        os.chdir(tempfile.mkdtemp())
        got = pcb_source.resolve_pcb_path(
            "missing/board.kicad_pcb", "/tmp/wd", config_path=cfg)
        assert not os.path.isfile(got)
        assert os.path.realpath(got) == os.path.realpath(
            os.path.join(os.getcwd(), "missing", "board.kicad_pcb"))
    finally:
        os.chdir(cwd)


def test_file_sha256_hashes_input_bytes():
    fd, path = tempfile.mkstemp()
    with os.fdopen(fd, "wb") as fh:
        fh.write(b"dcdc-tools")
    got = pcb_source.file_sha256(path)
    assert got == "474d28ed4d32e42f9077c162f3210eb518049d32eb8a2f1fbe1c03e229654f74"


def test_unknown_yaml_key_fails():
    cfg = _yaml("""
pcb: board.kicad_pcb
sw: SW
gnd: GND
out: out
bogus: 1
""")
    e = _expect_exit(lambda: extract_parasitics.parse_args(["--config", cfg]))
    assert e.code


def test_yaml_type_mismatch_fails():
    cfg = _yaml("""
pcb: board.kicad_pcb
sw: SW
gnd: GND
out: out
pitch: 2.0
""")
    e = _expect_exit(lambda: extract_parasitics.parse_args(["--config", cfg]))
    assert e.code


def test_invalid_copper_thickness_fails():
    cfg = _yaml("""
pcb: board.kicad_pcb
sw: SW
gnd: GND
out: out
cu_thickness: 0
""")
    e = _expect_exit(lambda: extract_parasitics.parse_args(["--config", cfg]))
    assert e.code


def test_invalid_terminal_mode_fails():
    cfg = _yaml("""
pcb: board.kicad_pcb
sw: SW
gnd: GND
out: out
    terminal_mode: magic
    """)
    e = _expect_exit(lambda: extract_parasitics.parse_args(["--config", cfg]))
    assert e.code


def test_invalid_cin_network_model_fails():
    cfg = _yaml("""
pcb: board.kicad_pcb
sw: SW
gnd: GND
out: out
cin_network_model: magic
""")
    e = _expect_exit(lambda: extract_parasitics.parse_args(["--config", cfg]))
    assert e.code


def test_resolved_only_cin_network_modes_are_not_user_requests():
    for mode in ("matrix_with_sw_coupling", "none"):
        cfg = _yaml(f"""
pcb: board.kicad_pcb
sw: SW
gnd: GND
out: out
cin_network_model: {mode}
""")
        e = _expect_exit(lambda: extract_parasitics.parse_args(["--config", cfg]))
        assert e.code == 2


def test_invalid_cin_extraction_basis_fails():
    cfg = _yaml("""
pcb: board.kicad_pcb
sw: SW
gnd: GND
out: out
cin_extraction_basis: magic
""")
    e = _expect_exit(lambda: extract_parasitics.parse_args(["--config", cfg]))
    assert e.code


def test_invalid_cin_closure_fails():
    cfg = _yaml("""
pcb: board.kicad_pcb
sw: SW
gnd: GND
out: out
cin_closure: magic
""")
    e = _expect_exit(lambda: extract_parasitics.parse_args(["--config", cfg]))
    assert e.code


def test_missing_required_merged_args_fail():
    cfg = _yaml("""
pcb: board.kicad_pcb
sw: SW
out: out
""")
    e = _expect_exit(lambda: extract_parasitics.parse_args(["--config", cfg]))
    assert e.code


def test_missing_gate_ports_hard_fails():
    side = {
        "ports": ["P_pwr", "P_bulk"],
        "topo": {"cin_dropped_ports": ["P_ghs", "P_gls"]},
    }
    e = _expect_exit(lambda: extract_parasitics.require_gate_ports(side, 2.0))
    msg = str(e.code)
    assert "missing gate-loop port(s)" in msg
    assert "P_ghs, P_gls" in msg
    assert "0.00 nH" in msg


def test_missing_gate_ports_can_warn_when_explicitly_allowed():
    side = {
        "ports": ["P_pwr", "P_bulk"],
        "topo": {"cin_dropped_ports": ["P_ghs", "P_gls"]},
    }
    extract_parasitics.require_gate_ports(
        side, 2.0, allow_missing_gate_ports=True)  # no raise


def test_cap_only_basis_does_not_require_gate_ports():
    side = {
        "ports": ["P_pwr"],
        "topo": {"cin_extraction_basis": "cap_only"},
    }
    extract_parasitics.require_gate_ports(side, 2.0)  # no raise


def test_switch_residual_basis_does_not_require_gate_ports():
    side = {
        "ports": ["P_sw_residual"],
        "topo": {"cin_extraction_basis": "switch_residual"},
    }
    extract_parasitics.require_gate_ports(side, 2.0)  # no raise


def test_matrix_solve_pitch_runs_three_bases_and_combines_payload():
    args = SimpleNamespace(
        emit_cin_network=True,
        cin_network_model="matrix",
        cin_extraction_basis="full_loop",
        plateau=5e6,
        cin_esl=0.0,
        cin_esr=0.0,
        config=None,
        pcb="/tmp/board.kicad_pcb",
    )
    calls = []
    cin_net = [
        {"ref": "C1", "cls": "mlcc", "label": "P_C1"},
        {"ref": "C2", "cls": "mlcc", "label": "P_C2"},
    ]

    def fake_run_geom(run_args, pitch, outdir, tag=None):
        basis = run_args.cin_extraction_basis
        calls.append((basis, tag))
        if basis == "switch_residual":
            return f"{outdir}/model_{tag}.inp", {
                "ports": ["P_swres"],
                "topo": {
                    "cin_extraction_basis": basis,
                    "demarcation_plane": {"switch_residual_port": "P_swres"},
                },
            }
        return f"{outdir}/model_{tag}.inp", {
            "ports": ["P_C1", "P_C2"],
            "cin_ports": ["P_C1", "P_C2"],
            "topo": {"cin_extraction_basis": basis, "cin_net": cin_net},
        }

    def fake_solve(inp, ports, topo, meta, plateau, suffix, cin_ports,
                   cin_esl, cin_esr):
        basis = topo["cin_extraction_basis"]
        if basis == "switch_residual":
            return {
                "ports": ports,
                "topo": topo,
                "port_L": [[0.7e-9]],
                "port_R": [[0.0]],
            }
        L_cap = [[4e-9, 1e-9], [1e-9, 9e-9]]
        L_full = [[4.9e-9, 2.1e-9], [2.1e-9, 10.3e-9]]
        return {
            "ports": ports,
            "topo": topo,
            "meta": meta,
            "port_L": L_full if basis == "full_loop" else L_cap,
            "port_R": [[1e-3, 0.0], [0.0, 2e-3]],
            "port_R_100k": [[3e-3, 0.0], [0.0, 4e-3]],
            "port_R_dc": [[0.7e-3, 0.0], [0.0, 1.1e-3]],
            "R_100k_freq_Hz": 1e5,
            "R_dc_freq_Hz": 1e3,
            "cin_model": {
                "mode": "scalar_trunk",
                "scalar_valid": False,
                "scalar_valid_basis": "homogeneity_only",
                "diagnostics": [{"severity": "error", "message": "scalar invalid"}],
                "region_assignment": {"basis": "full_loop_matrix_diagnostic"},
            },
            "cin_model_diagnostics": [{"severity": "error", "message": "scalar invalid"}],
        }

    _inp, _side, p = extract_parasitics.solve_pitch(
        args, 1.0, 0, "/tmp/work", "board.kicad_pcb", "pcbsha", None, None,
        run_geom_fn=fake_run_geom, solve_fn=fake_solve)
    assert calls == [
        ("full_loop", "full_loop"),
        ("cap_only", "cap_only"),
        ("switch_residual", "switch_residual"),
    ]
    cm = p["cin_model"]
    mx = p["cin_matrix"]
    assert cm["requested_mode"] == "matrix"
    assert cm["mode"] == "matrix_with_sw_coupling"
    assert cm["basis"] == "cap_only_additive"
    assert cm["matrix_valid"] is True
    assert p["cin_model_valid"] is True
    assert cm["region_assignment"]["basis"] == "cap_only_offdiag"
    assert cm["region_assignment"]["homogeneous"] is True
    assert cm["region_assignment"]["n_regions"] == 1
    assert cm["diagnostics"][0]["severity"] == "info"
    assert cm["diagnostics"][0]["scalar_context"] is True
    assert mx["gauge_fix_status"] == "fixed"
    assert mx["full_multiport_required"] is False
    assert len(mx["switch_couplings"]) == 2
    assert mx["R"] == [[3e-3, 0.0], [0.0, 4e-3]]
    assert mx["R_100k"] == [[3e-3, 0.0], [0.0, 4e-3]]
    assert mx["R_dc"] == [[0.7e-3, 0.0], [0.0, 1.1e-3]]
    assert mx["R_100k_freq_Hz"] == 1e5
    assert mx["R_dc_freq_Hz"] == 1e3
    floor = mx["separability_floor"]
    assert floor["status"] == "placeholder_pending_null_drop"
    assert floor["source"] == "placeholder_abs"
    assert floor["value"] == 0.05e-9
    assert mx["separability_fit"]["floor"] == floor["value"]
    assert mx["separability_fit"]["floor_source"] == floor["source"]
    assert mx["switch_separability"]["floor_status"] == floor["status"]
    assert cm["separability_floor"] == floor


def test_matrix_solve_pitch_uses_identity_payload_without_split_runs():
    args = SimpleNamespace(
        emit_cin_network=True,
        cin_network_model="matrix",
        cin_extraction_basis="full_loop",
        plateau=5e6,
        cin_esl=0.0,
        cin_esr=0.0,
        config=None,
        pcb="/tmp/board.kicad_pcb",
    )
    calls = []
    payload = {
        "basis": "identity",
        "refs": ["C1", "C2"],
        "L": [[4e-9, 1e-9], [1e-9, 9e-9]],
        "R": [[3e-3, 0.0], [0.0, 4e-3]],
        "R_100k": [[3e-3, 0.0], [0.0, 4e-3]],
        "R_dc": [[0.7e-3, 0.0], [0.0, 1.1e-3]],
        "L_sw_element": 0.0,
        "gauge_fix_status": "structurally_not_required",
        "gauge_fix_reason": "zero_by_plane_p_equiv",
        "switch_board_copper": "in_matrix",
        "kmax": 1.0 / 6.0,
        "spice_realizable": True,
    }

    def fake_run_geom(run_args, pitch, outdir, tag=None):
        calls.append((run_args.cin_extraction_basis, tag))
        return f"{outdir}/model_{tag}.inp", {
            "ports": ["P_C1", "P_C2"],
            "cin_ports": ["P_C1", "P_C2"],
            "topo": {"cin_extraction_basis": run_args.cin_extraction_basis},
        }

    def fake_solve(inp, ports, topo, meta, plateau, suffix, cin_ports,
                   cin_esl, cin_esr):
        return {
            "ports": ports,
            "topo": topo,
            "meta": meta,
            "cin_matrix": payload,
            "cin_model": {
                "mode": "matrix",
                "requested_mode": "matrix",
                "basis": "identity",
                "matrix_valid": True,
                "diagnostics": [],
            },
            "cin_model_valid": True,
            "cin_model_diagnostics": [],
        }

    _inp, _side, p = extract_parasitics.solve_pitch(
        args, 1.0, 0, "/tmp/work", "board.kicad_pcb", "pcbsha", None, None,
        run_geom_fn=fake_run_geom, solve_fn=fake_solve)
    assert calls == [("full_loop", "full_loop")]
    assert p["cin_model_valid"] is True
    assert p["cin_model"]["basis"] == "identity"
    assert p["cin_matrix"] is payload


def test_matrix_solve_pitch_rejects_invalid_identity_without_split_fallback():
    args = SimpleNamespace(
        emit_cin_network=True,
        cin_network_model="matrix",
        cin_extraction_basis="full_loop",
        plateau=5e6,
        cin_esl=0.0,
        cin_esr=0.0,
        config=None,
        pcb="/tmp/board.kicad_pcb",
    )
    calls = []

    def fake_run_geom(run_args, pitch, outdir, tag=None):
        calls.append((run_args.cin_extraction_basis, tag))
        return f"{outdir}/model_{tag}.inp", {
            "ports": ["P_C1", "P_C2"],
            "cin_ports": ["P_C1", "P_C2"],
            "topo": {"cin_extraction_basis": run_args.cin_extraction_basis},
        }

    def fake_solve(inp, ports, topo, meta, plateau, suffix, cin_ports,
                   cin_esl, cin_esr):
        return {
            "ports": ports,
            "topo": topo,
            "meta": meta,
            "cin_matrix": {
                "basis": "identity",
                "refs": ["C1", "C2"],
                "L": [[4e-9, 1e-9], [1e-9, 9e-9]],
                "R": [[3e-3, 0.0], [0.0, 4e-3]],
                "L_sw_element": 0.0,
                "gauge_fix_status": "structurally_not_required",
                "gauge_fix_reason": "zero_by_plane_p_equiv",
                "switch_board_copper": "in_matrix",
                "kmax": 0.96,
                "spice_realizable": False,
            },
            "cin_model": {
                "mode": "matrix",
                "requested_mode": "matrix",
                "basis": "identity",
                "matrix_valid": False,
                "diagnostics": [],
            },
            "cin_model_valid": False,
            "cin_model_diagnostics": [],
        }

    try:
        extract_parasitics.solve_pitch(
            args, 1.0, 0, "/tmp/work", "board.kicad_pcb", "pcbsha", None, None,
            run_geom_fn=fake_run_geom, solve_fn=fake_solve)
    except RuntimeError as e:
        assert "identity matrix Cin payload is invalid" in str(e)
    else:
        raise AssertionError("expected invalid identity payload to fail closed")
    assert calls == [("full_loop", "full_loop")]


def test_cin_separability_floor_metadata_is_calibration_not_knob():
    placeholder = extract_parasitics._cin_separability_floor_metadata()
    assert placeholder["status"] == "placeholder_pending_null_drop"
    assert placeholder["source"] == "placeholder_abs"
    assert placeholder["value"] == 0.05e-9
    assert "not a user" in placeholder["reason"]

    calibrated = extract_parasitics._cin_separability_floor_metadata(
        null_scatter=0.02e-9, gmres_floor=0.03e-9)
    assert calibrated["status"] == "calibrated"
    assert calibrated["source"] == "same_fixture_null_drop"
    assert calibrated["value"] == 0.03e-9


def test_matrix_region_assignment_uses_cap_only_offdiag_axis():
    payload = {
        "refs": ["C16", "C17", "C27"],
        "L": [
            [4.0e-9, 3.2e-9, 0.56e-9],
            [3.2e-9, 4.2e-9, 0.58e-9],
            [0.56e-9, 0.58e-9, 6.0e-9],
        ],
        "separability_floor": {"value": 0.05e-9},
    }
    ra = extract_parasitics._cap_only_region_assignment(
        payload, payload["separability_floor"])
    assert ra["basis"] == "cap_only_offdiag"
    assert ra["status"] == "heterogeneous"
    assert ra["homogeneous"] is False
    assert ra["n_regions"] == 2
    weak = [r["ref"] for r in ra["regions"] if r["weak_region"]]
    assert weak == ["C27"]


def test_matrix_full_multiport_fallback_is_explicitly_refused():
    full_p = {
        "cin_model": {
            "mode": "scalar_trunk",
            "scalar_valid": False,
            "diagnostics": [{"severity": "error", "message": "scalar invalid"}],
            "region_assignment": {"basis": "full_loop_matrix_diagnostic"},
        },
        "cin_model_diagnostics": [{"severity": "error", "message": "scalar invalid"}],
    }
    payload = {
        "mode": "none",
        "basis": "cap_only_additive",
        "refs": ["C1", "C2"],
        "L": [[1e-9, 0.2e-9], [0.2e-9, 1.1e-9]],
        "R": [[1e-3, 0.0], [0.0, 1e-3]],
        "L_sw_physical": 0.7e-9,
        "m_i_physical": [0.0, 0.0],
        "regauge_c": 0.0,
        "m_i_modeling": [0.0, 0.0],
        "switch_couplings": [],
        "separability_fit": {"residual_fro": 0.2e-9, "floor": 0.05e-9},
        "switch_separability": {
            "status": "failed",
            "reason": "additive_fit_residual_above_floor",
        },
        "gauge_fix_status": "fixed",
        "gauge_fix_reason": "explicit_switch_residual_port",
        "switch_board_copper": "nonseparable_full_multiport_required",
        "full_multiport_required": True,
        "full_multiport_valid": False,
        "full_multiport_reason": "switch_separability_failed_additive_residual_above_floor",
        "decomposition_valid": False,
        "spice_realizable": False,
        "separability_floor": {
            "value": 0.05e-9,
            "source": "placeholder_abs",
            "status": "placeholder_pending_null_drop",
        },
    }
    ok, reason = extract_parasitics._matrix_valid_for_payload(payload)
    assert ok is False
    assert "full multiport fallback" in reason

    p = extract_parasitics._apply_cin_matrix_payload(full_p, payload, "matrix")
    cm = p["cin_model"]
    assert cm["mode"] == "none"
    assert cm["matrix_valid"] is False
    assert cm["full_multiport_required"] is True
    assert cm["full_multiport_valid"] is False
    assert p["cin_model_valid"] is False
    assert cm["diagnostics"][-1]["code"] == "cin_full_multiport_required"
    assert "full multiport fallback" in cm["diagnostics"][-1]["message"]


def test_matrix_payload_validity_refuses_malformed_numeric_payload():
    payload = {
        "mode": "matrix_with_sw_coupling",
        "basis": "cap_only_additive",
        "refs": ["C1", "C2"],
        "L": [[1e-9, -2e-9], [-2e-9, 1e-9]],
        "R": [[1e-3, 0.0], [0.0, 1e-3]],
        "L_sw_element": 0.7e-9,
        "gauge_fix_status": "fixed",
        "switch_board_copper": "split_lsw_element",
        "full_multiport_required": False,
        "decomposition_valid": True,
        "spice_realizable": True,
    }
    ok, reason = extract_parasitics._matrix_valid_for_payload(payload)
    assert ok is False
    assert "positive semidefinite" in reason

    payload["L"] = [[1e-9, 0.0], [0.0, 1e-9]]
    payload["R"] = [[-1e-3, 0.0], [0.0, 1e-3]]
    ok, reason = extract_parasitics._matrix_valid_for_payload(payload)
    assert ok is False
    assert "negative self R" in reason

    payload["R"] = [[1e-3, 0.0], [0.0, 1e-3]]
    payload["L"] = [[1e-9, 0.96e-9], [0.96e-9, 1e-9]]
    ok, reason = extract_parasitics._matrix_valid_for_payload(payload)
    assert ok is False
    assert "at/above 0.95" in reason

    payload["L"] = [[1e-9, 0.0], [0.0, 1e-9]]
    payload["switch_couplings"] = [{"ref": "C3", "K": 0.1}]
    ok, reason = extract_parasitics._matrix_valid_for_payload(payload)
    assert ok is False
    assert "not in refs" in reason

    payload["switch_couplings"] = []
    payload["gauge_fix_status"] = "missing"
    ok, reason = extract_parasitics._matrix_valid_for_payload(payload)
    assert ok is False
    assert "gauge_fix_status" in reason

    payload["gauge_fix_status"] = "fixed"
    payload["R_dc"] = [[1e-3, 0.0]]
    ok, reason = extract_parasitics._matrix_valid_for_payload(payload)
    assert ok is False
    assert "R_dc matrix shape" in reason

    identity = {
        "basis": "identity",
        "refs": ["C1", "C2"],
        "L": [[1e-9, 0.0], [0.0, 1e-9]],
        "R": [[1e-3, 0.0], [0.0, 1e-3]],
        "R_100k": [[1e-3, 2e-4], [1e-4, 1e-3]],
        "R_dc": [[1e-3, 0.0], [0.0, 1e-3]],
        "L_sw_element": 0.0,
        "gauge_fix_status": "structurally_not_required",
        "switch_board_copper": "in_matrix",
        "spice_realizable": True,
    }
    ok, reason = extract_parasitics._matrix_valid_for_payload(identity)
    assert ok is False
    assert "R_100k matrix is not symmetric" in reason

    identity["R_100k"] = [[-1e-3, 0.0], [0.0, 1e-3]]
    ok, reason = extract_parasitics._matrix_valid_for_payload(identity)
    assert ok is False
    assert "negative self R_100k" in reason


def test_present_gate_ports_pass():
    side = {"ports": ["P_pwr", "P_ghs", "P_gls"], "topo": {}}
    extract_parasitics.require_gate_ports(side, 2.0)  # no raise


def test_per_device_gate_ports_are_required_from_manifest():
    side = {
        "ports": ["P_pwr", "P_ghs_Q1", "P_gls_Q2"],
        "topo": {
            "parallel_fets": "per-device",
            "hs": {"device_ports": [
                {"ref": "Q1", "gate_label": "P_ghs_Q1"},
                {"ref": "Q3", "gate_label": "P_ghs_Q3"},
            ]},
            "ls": {"device_ports": [
                {"ref": "Q2", "gate_label": "P_gls_Q2"},
            ]},
        },
    }
    e = _expect_exit(lambda: extract_parasitics.require_gate_ports(side, 2.0))
    assert "P_ghs_Q3" in str(e.code)


def test_per_device_gate_ports_can_warn_when_explicitly_allowed():
    side = {
        "ports": ["P_pwr", "P_ghs_Q1", "P_gls_Q2"],
        "topo": {
            "parallel_fets": "per-device",
            "hs": {"device_ports": [
                {"ref": "Q1", "gate_label": "P_ghs_Q1"},
                {"ref": "Q3", "gate_label": "P_ghs_Q3"},
            ]},
            "ls": {"device_ports": [
                {"ref": "Q2", "gate_label": "P_gls_Q2"},
            ]},
        },
    }
    extract_parasitics.require_gate_ports(
        side, 2.0, allow_missing_gate_ports=True)  # no raise


def test_per_device_gate_ports_pass_when_all_present():
    side = {
        "ports": ["P_pwr", "P_ghs_Q1", "P_ghs_Q3", "P_gls_Q2"],
        "topo": {
            "parallel_fets": "per-device",
            "hs": {"device_ports": [
                {"ref": "Q1", "gate_label": "P_ghs_Q1"},
                {"ref": "Q3", "gate_label": "P_ghs_Q3"},
            ]},
            "ls": {"device_ports": [
                {"ref": "Q2", "gate_label": "P_gls_Q2"},
            ]},
        },
    }
    extract_parasitics.require_gate_ports(side, 2.0)  # no raise


def test_inject_packages_stamps_topo_when_cli_supplied():
    args = SimpleNamespace(hs_package="TO-220", ls_package="TO-247")
    side = {"topo": {"hs": {"refs": ["Q1"]}, "ls": {"refs": ["Q2"]}}}
    extract_parasitics._inject_packages(args, side)
    assert side["topo"]["hs"]["package"] == "TO-220", side
    assert side["topo"]["ls"]["package"] == "TO-247", side


def test_inject_packages_noop_when_absent_or_missing_side():
    # No packages supplied -> topo untouched.
    args = SimpleNamespace(hs_package=None, ls_package=None)
    side = {"topo": {"hs": {"refs": ["Q1"]}}}
    extract_parasitics._inject_packages(args, side)
    assert "package" not in side["topo"]["hs"], side
    # Only one side supplied, and the other side's dict is absent -> no crash,
    # only the present side is stamped.
    args = SimpleNamespace(hs_package="DFN5x6")
    side = {"topo": {"hs": {"refs": ["Q1"]}}}  # no "ls" key
    extract_parasitics._inject_packages(args, side)
    assert side["topo"]["hs"]["package"] == "DFN5x6", side
    assert "ls" not in side["topo"], side


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for t in tests:
        try:
            t()
            print(f"  ok   {t.__name__}")
        except AssertionError as e:
            fails += 1
            print(f"  FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            fails += 1
            print(f"  ERR  {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - fails}/{len(tests)} passed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
