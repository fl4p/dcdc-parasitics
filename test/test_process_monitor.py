#!/usr/bin/env python3
from dataclasses import asdict
import json
import os
import pathlib
import sys
import time

import psutil
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "lib"))

import process_monitor  # noqa: E402
from process_monitor import (  # noqa: E402
    ProcessLimits,
    _discover_descendants,
    _platform_command,
    _tracked_members,
    run_monitored_process,
    validate_process_event_witness,
)
from windows_job_runner import _assign_resume_wait  # noqa: E402


def _limits(**overrides):
    values = {
        "wall_time_s": 5.0,
        "peak_rss_bytes": 1024**3,
        "output_bytes": 1024**2,
        "refined_panels": 1000,
        "gmres_iterations_per_rhs": 1000,
    }
    values.update(overrides)
    return ProcessLimits(**values)


# --------------------------------------------------------------------------- #
# the memory metric
#
# These calibrate the 2026-08-27 change from resident set size to memory
# footprint. The defect they answer to: a Palace run held 38.5 GB of swap on a
# 36 GB machine while reporting about 6 GB of RSS against a 24 GiB ceiling that
# never fired, because the resident set is by definition the part that stays in
# RAM and so falls as the situation gets worse.
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(sys.platform != "darwin", reason="reads a Darwin struct")
def test_the_struct_offset_really_is_the_footprint_field():
    """A wrong offset returns a plausible number from a neighbouring field.

    ri_resident_size sits immediately before ri_phys_footprint in rusage_info,
    and it is independently observable as psutil's RSS. If reading one field
    earlier reproduces RSS exactly, the layout is right and the field the module
    reads is the one it means to.
    """
    import ctypes

    buffer = ctypes.create_string_buffer(process_monitor._RUSAGE_INFO_V0_BYTES)
    assert process_monitor._proc_pid_rusage(
        os.getpid(), process_monitor._RUSAGE_INFO_V0, ctypes.byref(buffer)) == 0
    offset = process_monitor._PHYS_FOOTPRINT_OFFSET - 8
    resident = int.from_bytes(buffer.raw[offset:offset + 8], sys.byteorder)
    assert resident == psutil.Process(os.getpid()).memory_info().rss


def test_the_footprint_tracks_dirty_anonymous_memory():
    """The pages that get compressed and swapped are the ones that must count.

    Footprint is not RSS and is not asserted to be: it excludes clean
    file-backed pages. What it must do is rise with anonymous dirty memory,
    because that is the memory a hog takes to the swap file.
    """
    megabytes = 192
    before = process_monitor._process_footprint(os.getpid())
    assert before is not None
    ballast = bytearray(megabytes * 1024**2)
    ballast[::4096] = b"\x01" * len(ballast[::4096])       # touch every page
    after = process_monitor._process_footprint(os.getpid())
    del ballast
    assert after - before >= megabytes * 1024**2 * 0.75


def test_a_process_that_is_gone_measures_zero_not_unreadable():
    """The one error that legitimately means nothing is being used."""
    assert process_monitor._process_footprint(2**21 - 1) == 0


def test_an_unreadable_member_raises_rather_than_counting_as_zero(monkeypatch):
    """Unevaluable is not OK. It is the whole reason the old code was wrong.

    The previous implementation caught the read failure per member and moved on,
    so a tree it could not measure reported a smaller number -- fail-open at
    exactly the moment the machine is least able to answer questions about
    processes.
    """
    monkeypatch.setattr(process_monitor, "_process_footprint", lambda pid: None)
    with pytest.raises(process_monitor.MemoryMetricUnavailable):
        process_monitor._tree_memory(
            psutil.Process(os.getpid()), psutil.Process(os.getpid()).create_time(),
            {}, "token-that-matches-nothing")


def test_an_unmeasurable_platform_refuses_to_launch_at_all(tmp_path, monkeypatch):
    """A precondition, not a discovery made after the tree already exists."""
    monkeypatch.setattr(process_monitor, "_process_footprint", None)
    with pytest.raises(process_monitor.MemoryMetricUnavailable):
        run_monitored_process(
            [sys.executable, "-c", "pass"], cwd=tmp_path, limits=_limits())


def test_the_memory_failure_no_longer_claims_to_be_about_rss():
    """Provenance: the message must name the quantity that was bounded."""
    source = (
        pathlib.Path(process_monitor.__file__).read_text())
    assert "peak RSS exceeded" not in source
    assert "peak memory footprint exceeded" in source
    assert process_monitor.MEMORY_METRIC in (
        "darwin:ri_phys_footprint", "linux:VmRSS+VmSwap")


def test_monitor_exception_kills_and_reaps_started_process(tmp_path, monkeypatch):
    pid_path = tmp_path / "child.pid"
    real_tree_memory = process_monitor._tree_memory
    injected = False

    def fail_after_start(*args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            raise RuntimeError("injected monitor failure")
        return real_tree_memory(*args, **kwargs)

    monkeypatch.setattr(process_monitor, "_tree_memory", fail_after_start)
    with pytest.raises(RuntimeError, match="injected monitor failure"):
        run_monitored_process(
            [sys.executable, "-c", (
                "from pathlib import Path; import os,time; "
                "Path(os.environ['PID_PATH']).write_text(str(os.getpid())); "
                "time.sleep(60)"
            )],
            cwd=tmp_path,
            limits=_limits(),
            environment={**os.environ, "PID_PATH": str(pid_path)},
        )
    if pid_path.exists():
        assert not psutil.pid_exists(int(pid_path.read_text()))


def test_secondary_cleanup_interrupt_is_tagged_while_process_survives(
        tmp_path, monkeypatch):
    monkeypatch.setattr(
        process_monitor, "_tree_memory",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("monitor failed")),
    )
    monkeypatch.setattr(
        process_monitor, "_cleanup_exceptional_process",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(process_monitor, "_tree_alive", lambda *args: True)
    token = process_monitor.reset_solver_tree_state()
    try:
        with pytest.raises(
                process_monitor.ProcessCleanupError,
                match="interrupted before tree death"):
            run_monitored_process(
                [sys.executable, "-c", "import time; time.sleep(0.1)"],
                cwd=tmp_path,
                limits=_limits(),
            )
        assert process_monitor.solver_tree_may_be_alive()
    finally:
        process_monitor.restore_solver_tree_state(token)


def test_solver_tree_state_clears_after_normal_completion(tmp_path):
    token = process_monitor.reset_solver_tree_state()
    try:
        assert not process_monitor.solver_tree_may_be_alive()
        run_monitored_process(
            [sys.executable, "-c", "pass"], cwd=tmp_path, limits=_limits(),
        )
        assert not process_monitor.solver_tree_may_be_alive()
    finally:
        process_monitor.restore_solver_tree_state(token)


def test_additional_output_path_is_included_in_disk_limit(tmp_path):
    checkpoint = tmp_path / "checkpoint"
    checkpoint.mkdir()
    cwd = tmp_path / "run"
    cwd.mkdir()
    result = run_monitored_process(
        [sys.executable, "-c", (
            "from pathlib import Path; import sys; "
            "Path(sys.argv[1]).write_bytes(b'x' * 4096)"
        ), str(checkpoint / "shard.bin")],
        cwd=cwd,
        limits=_limits(output_bytes=1024),
        additional_output_paths=(checkpoint,),
    )
    assert any(failure.startswith("output exceeded ")
               for failure in result.limit_failures)
    assert result.directory_growth_bytes >= 4096


def test_stream_and_monitor_event_witnesses_cover_captured_bytes(tmp_path):
    result = run_monitored_process(
        [sys.executable, "-c", (
            "import sys; sys.stdout.write('out'); sys.stdout.flush(); "
            "sys.stderr.write('err'); sys.stderr.flush()"
        )],
        cwd=tmp_path,
        limits=_limits(),
    )
    execution = asdict(result)
    execution.pop("stdout")
    execution.pop("stderr")
    execution = json.loads(json.dumps(execution))
    assert validate_process_event_witness(
        execution, result.stdout, result.stderr
    )
    assert {event["source"] for event in execution["stream_events"]} == {
        "stdout", "stderr"
    }
    assert [event["event"] for event in execution["monitor_events"]] == [
        "prelaunch", "process_started", "process_exited"
    ]

    rebound = json.loads(json.dumps(execution))
    rebound["stream_events"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="content mismatch"):
        validate_process_event_witness(rebound, result.stdout, result.stderr)

    negative_time = json.loads(json.dumps(execution))
    negative_time["stream_events"][0]["monotonic_ns"] = -1
    with pytest.raises(ValueError, match="values are invalid"):
        validate_process_event_witness(
            negative_time, result.stdout, result.stderr
        )

    before_start = json.loads(json.dumps(execution))
    before_start["stream_events"][0]["monotonic_ns"] = 0
    with pytest.raises(ValueError, match="outside process lifetime"):
        validate_process_event_witness(
            before_start, result.stdout, result.stderr
        )

    rebound_exit = json.loads(json.dumps(execution))
    rebound_exit["monitor_events"][-1]["detail"] = "99"
    with pytest.raises(ValueError, match="exit detail"):
        validate_process_event_witness(
            rebound_exit, result.stdout, result.stderr
        )

    reordered = json.loads(json.dumps(execution))
    reordered["monitor_events"][1], reordered["monitor_events"][2] = (
        reordered["monitor_events"][2], reordered["monitor_events"][1]
    )
    with pytest.raises(ValueError, match="event order"):
        validate_process_event_witness(reordered, result.stdout, result.stderr)


def test_windows_commands_use_suspended_job_object_runner():
    command = ("FasterCap.exe", "model.lst", "-b")
    wrapped = _platform_command(command, platform_name="nt")
    assert wrapped[0] == sys.executable
    assert wrapped[1].endswith("windows_job_runner.py")
    assert tuple(json.loads(wrapped[2])) == command


def test_monitored_record_preserves_original_command_when_wrapped(
        tmp_path, monkeypatch):
    original = (sys.executable, "-c", "print('original')")
    wrapped = (sys.executable, "-c", "print('wrapped')")
    monkeypatch.setattr(
        process_monitor, "_platform_command", lambda _command: wrapped,
    )
    result = run_monitored_process(
        original, cwd=tmp_path, limits=_limits(),
    )
    assert result.command == original
    assert result.stdout == b"wrapped\n"


def test_windows_job_waits_for_all_assigned_processes_after_leader_exit():
    events = []
    active = iter((2, 1, 0))

    class Api:
        @staticmethod
        def TerminateProcess(process, code):
            raise AssertionError((process, code))

    class Event:
        INFINITE = -1

        @staticmethod
        def WaitForSingleObject(process, timeout):
            events.append(("wait", process, timeout))

    class Job:
        JobObjectBasicAccountingInformation = "accounting"

        @staticmethod
        def AssignProcessToJobObject(job, process):
            events.append(("assign", job, process))

        @staticmethod
        def QueryInformationJobObject(job, information):
            events.append(("query", job, information))
            return {"ActiveProcesses": next(active)}

    class Process:
        @staticmethod
        def ResumeThread(thread):
            events.append(("resume", thread))

        @staticmethod
        def GetExitCodeProcess(process):
            events.append(("exit", process))
            return 7

    result = _assign_resume_wait(
        "job", "process", "thread",
        win32api=Api, win32event=Event, win32job=Job,
        win32process=Process,
        sleep=lambda seconds: events.append(("sleep", seconds)),
    )
    assert result == 7
    assert events.count(("sleep", 0.02)) == 2
    assert events[-1] == ("query", "job", "accounting")


def test_windows_job_assignment_failure_terminates_suspended_target():
    events = []

    class Api:
        @staticmethod
        def TerminateProcess(process, code):
            events.append(("terminate", process, code))

    class Event:
        INFINITE = -1

        @staticmethod
        def WaitForSingleObject(process, timeout):
            events.append(("wait", process, timeout))

    class Job:
        @staticmethod
        def AssignProcessToJobObject(job, process):
            raise RuntimeError("assignment failed")

    class Process:
        @staticmethod
        def ResumeThread(thread):
            events.append(("resume", thread))

    with pytest.raises(RuntimeError, match="assignment failed"):
        _assign_resume_wait(
            "job", "process", "thread",
            win32api=Api, win32event=Event, win32job=Job,
            win32process=Process,
        )
    assert events == [
        ("terminate", "process", 1),
        ("wait", "process", Event.INFINITE),
    ]


def test_recycled_pid_is_removed_from_tracked_identity(monkeypatch):
    class ReusedProcess:
        def __init__(self, pid):
            self.pid = pid

        @staticmethod
        def create_time():
            return 2.0

    monkeypatch.setattr(psutil, "Process", ReusedProcess)
    tracked = {111: 1.0}
    assert _tracked_members(tracked) == []
    assert tracked == {}


def test_recycled_leader_pid_is_not_used_as_a_descendant_root(monkeypatch):
    class ReusedLeader:
        def __init__(self, pid):
            self.pid = pid

        @staticmethod
        def create_time():
            return 2.0

        @staticmethod
        def children(recursive=False):
            raise AssertionError(recursive)

    class Popen:
        pid = 4242

    monkeypatch.setattr(psutil, "Process", ReusedLeader)
    tracked = {}
    _discover_descendants(Popen(), 1.0, tracked)
    assert tracked == {}


def test_limit_repeats_tree_kill_until_tree_is_gone(tmp_path, monkeypatch):
    real_kill = process_monitor._kill_tree
    calls = 0

    def counted_kill(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_kill(*args, **kwargs)

    monkeypatch.setattr(process_monitor, "_kill_tree", counted_kill)
    output = run_monitored_process(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        cwd=tmp_path,
        limits=_limits(wall_time_s=0.05),
    )
    assert output.limit_failures
    assert calls >= 2


def test_stream_limits_stop_output_panels_and_rss(tmp_path):
    output = run_monitored_process(
        [sys.executable, "-c", "import os,time; os.write(1,b'x'*100000000); time.sleep(1)"],
        cwd=tmp_path,
        limits=_limits(output_bytes=1024),
    )
    assert any("output exceeded" in failure for failure in output.limit_failures)
    assert output.output_bytes < 100000000

    panels = run_monitored_process(
        [sys.executable, "-c",
         "import sys,time; print('Number of panels after refinement: 1001', flush=True); time.sleep(1)"],
        cwd=tmp_path,
        limits=_limits(),
    )
    assert panels.max_refined_panels == 1001
    assert any("refined panels exceeded" in failure
               for failure in panels.limit_failures)

    memory = run_monitored_process(
        [sys.executable, "-c", "import time; value=bytearray(20_000_000); time.sleep(1)"],
        cwd=tmp_path,
        limits=_limits(peak_rss_bytes=5_000_000),
    )
    assert memory.peak_rss_bytes > 5_000_000
    assert any("peak memory footprint exceeded" in failure
               for failure in memory.limit_failures)


@pytest.mark.parametrize(
    ("line", "overrides", "failure_text"),
    (
        (
            "Number of panels after refinement: 1001",
            {},
            "refined panels exceeded",
        ),
        (
            "GMRES Iteration: 0 1 2 3 4",
            {"gmres_iterations_per_rhs": 5},
            "GMRES iteration limit",
        ),
    ),
)
def test_sub_64k_stream_limit_terminates_process_promptly(
        tmp_path, line, overrides, failure_text):
    started = time.monotonic()
    result = run_monitored_process(
        [
            sys.executable,
            "-c",
            f"import time; print({line!r}, flush=True); time.sleep(4)",
        ],
        cwd=tmp_path,
        limits=_limits(**overrides),
    )
    elapsed = time.monotonic() - started
    assert len(result.stdout) < 65536
    assert elapsed < 2.0
    assert result.returncode != 0
    assert any(failure_text in failure for failure in result.limit_failures)


@pytest.mark.parametrize(
    ("first", "second", "overrides", "failure_text"),
    (
        (
            "Number of panels after refine",
            "ment: 1001\n",
            {},
            "refined panels exceeded",
        ),
        (
            "GMRES Iteration: 0 1 ",
            "2 3 4\n",
            {"gmres_iterations_per_rhs": 5},
            "GMRES iteration limit",
        ),
    ),
)
def test_split_stdout_limit_survives_interleaved_stderr(
        tmp_path, first, second, overrides, failure_text):
    code = (
        "import os,time; "
        f"os.write(1,{first.encode()!r}); time.sleep(0.1); "
        "os.write(2,b'interleaved stderr\\n'); time.sleep(0.1); "
        f"os.write(1,{second.encode()!r}); time.sleep(4)"
    )
    started = time.monotonic()
    result = run_monitored_process(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        limits=_limits(**overrides),
    )
    elapsed = time.monotonic() - started
    assert (first + second).encode() in result.stdout
    assert b"interleaved stderr" in result.stderr
    assert elapsed < 2.0
    assert result.returncode != 0
    assert any(failure_text in failure for failure in result.limit_failures)


def test_postexit_output_limit_has_a_self_consistent_witness(tmp_path):
    result = run_monitored_process(
        [sys.executable, "-c",
         "import pathlib; pathlib.Path('fast.tmp').write_bytes(b'x'*200000)"],
        cwd=tmp_path,
        limits=_limits(output_bytes=100_000),
    )
    assert result.limit_failures
    assert [event["event"] for event in result.monitor_events] in (
        ["prelaunch", "process_started", "postexit_limit_detected",
         "process_exited"],
        ["prelaunch", "process_started", "limit_detected",
         "kill_initiated", "process_exited"],
    )
    execution = asdict(result)
    execution.pop("stdout")
    execution.pop("stderr")
    assert validate_process_event_witness(
        json.loads(json.dumps(execution)), result.stdout, result.stderr
    )


def test_directory_and_gmres_limits_include_incomplete_iteration(tmp_path):
    disk = run_monitored_process(
        [sys.executable, "-c",
         "import pathlib,time; pathlib.Path('large.tmp').write_bytes(b'x'*200000); time.sleep(1)"],
        cwd=tmp_path,
        limits=_limits(output_bytes=100_000),
    )
    assert disk.directory_growth_bytes >= 200_000
    assert any("output exceeded" in failure for failure in disk.limit_failures)

    gmres = run_monitored_process(
        [sys.executable, "-c",
         "import time; print('GMRES Iteration: 0 1 2 3 4', flush=True); time.sleep(1)"],
        cwd=tmp_path,
        limits=_limits(gmres_iterations_per_rhs=5),
    )
    assert gmres.max_gmres_iteration == 4
    assert any("GMRES iteration limit" in failure
               for failure in gmres.limit_failures)
    execution = asdict(gmres)
    execution.pop("stdout")
    execution.pop("stderr")
    execution["limit_failures"] = []
    execution["monitor_events"] = [
        execution["monitor_events"][0], execution["monitor_events"][1],
        execution["monitor_events"][-1],
    ]
    with pytest.raises(ValueError, match="limit causes"):
        validate_process_event_witness(
            json.loads(json.dumps(execution)), gmres.stdout, gmres.stderr
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object integration")
def test_windows_job_object_kills_orphan_after_leader_exit(tmp_path):
    code = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(2)'])"
    )
    started = time.monotonic()
    result = run_monitored_process(
        [sys.executable, "-c", code], cwd=tmp_path,
        limits=_limits(wall_time_s=0.15),
    )
    assert time.monotonic() - started < 0.8
    assert any("wall time exceeded" in item for item in result.limit_failures)


def test_wall_limit_kills_orphaned_descendant_after_leader_exits(tmp_path):
    child_path = tmp_path / "orphan.pid"
    code = (
        "import pathlib,subprocess,sys; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(2)']); "
        f"pathlib.Path({str(child_path)!r}).write_text(str(child.pid))"
    )
    started = time.monotonic()
    result = run_monitored_process(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        limits=_limits(wall_time_s=0.15),
    )
    elapsed = time.monotonic() - started
    assert elapsed < 0.8
    assert any("wall time exceeded" in failure for failure in result.limit_failures)
    child_pid = int(child_path.read_text())
    if psutil.pid_exists(child_pid):
        assert psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE


def test_rss_limit_includes_reparented_new_session_grandchild(tmp_path):
    grandchild = "import time; value=bytearray(256_000_000); time.sleep(0.8)"
    intermediate = (
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable,'-c',{grandchild!r}],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL,start_new_session=True)"
    )
    leader = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-c',{intermediate!r}],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL,start_new_session=True); time.sleep(2)"
    )
    limit = 160 * 1024**2
    started = time.monotonic()
    result = run_monitored_process(
        [sys.executable, "-c", leader],
        cwd=tmp_path,
        limits=_limits(peak_rss_bytes=limit),
    )
    assert time.monotonic() - started < 3.0
    assert result.peak_rss_bytes > limit
    assert any("peak memory footprint exceeded" in item
               for item in result.limit_failures)


def test_wall_limit_tracks_orphan_with_redirected_standard_streams(tmp_path):
    child_path = tmp_path / "redirected-orphan.pid"
    code = (
        "import pathlib,subprocess,sys; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(4)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL,start_new_session=True); "
        f"pathlib.Path({str(child_path)!r}).write_text(str(child.pid))"
    )
    started = time.monotonic()
    result = run_monitored_process(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        limits=_limits(wall_time_s=0.3),
    )
    elapsed = time.monotonic() - started
    assert elapsed < 1.5
    assert any("wall time exceeded" in failure for failure in result.limit_failures)
    child_pid = int(child_path.read_text())
    if psutil.pid_exists(child_pid):
        assert psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE


def test_wall_limit_kills_descendant_process_tree(tmp_path):
    child_path = tmp_path / "child.pid"
    code = (
        "import pathlib,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        f"pathlib.Path({str(child_path)!r}).write_text(str(child.pid)); "
        "time.sleep(30)"
    )
    result = run_monitored_process(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        limits=_limits(wall_time_s=0.2),
    )
    assert any("wall time exceeded" in failure for failure in result.limit_failures)
    child_pid = int(child_path.read_text())
    deadline = time.monotonic() + 2
    while psutil.pid_exists(child_pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    if psutil.pid_exists(child_pid):
        process = psutil.Process(child_pid)
        assert process.status() == psutil.STATUS_ZOMBIE
