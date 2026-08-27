#!/usr/bin/env python3
"""Bounded subprocess execution with byte-exact stream capture."""
import contextvars
import ctypes
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import uuid

import psutil


MONITOR_TOKEN_ENV = "DCDC_PROCESS_MONITOR_TOKEN"


class ProcessCleanupError(RuntimeError):
    pass


class WallClockUnavailable(RuntimeError):
    """No sleep-inclusive clock, so elapsed time could not be measured.

    Raised rather than falling back to `time.monotonic`. A wall-time limit
    enforced on a clock that stops when the machine sleeps is not a wall-time
    limit; it silently grants the run however long the lid was shut.
    """


class MemoryMetricUnavailable(RuntimeError):
    """The tree's memory could not be measured, so no bound can be asserted.

    Raised rather than tolerated. A member whose memory cannot be read is not a
    member using none, and a run whose peak memory is unknown has not been shown
    to fit under a ceiling -- it has only failed to be shown to exceed one.
    """


# What the `peak_rss_bytes` field of every witness holds since 2026-08-27.
#
# The name is not the metric, and the name is the one that could not change: it
# is baked into the resource-sample schema of every run manifest already on
# disk, all of which are validated against an exact key set. The quantity it
# names did change, from resident set size to the platform's memory footprint,
# because RSS cannot bound a process that swaps. The resident set is by
# definition the part that stays in RAM, so it *falls* as the situation gets
# worse: a Palace run held 38.5 GB of swap on a 36 GB machine -- past the
# excursion that had panicked the same machine the day before -- while reporting
# about 6 GB of RSS against a 24 GiB ceiling that never came close to firing.
#
# Footprint does not compare to RSS in either direction. It excludes clean
# file-backed pages, so an idle process measures smaller than its RSS, and it
# includes compressed and swapped pages, so a hog measures larger. Peak figures
# recorded before 2026-08-27 are RSS and are not comparable to later ones.
_RUSAGE_INFO_V0 = 0
_RUSAGE_INFO_V0_BYTES = 16 + 10 * 8        # ri_uuid[16] then ten uint64 fields
_PHYS_FOOTPRINT_OFFSET = 16 + 7 * 8        # the eighth of those fields
_ESRCH = 3


def _darwin_footprint(pid):
    """ri_phys_footprint for one pid: bytes, 0 if gone, None if unreadable.

    This is the quantity jetsam kills on and Activity Monitor reports as
    "Memory". ESRCH is the single error mapped to zero, because a process that
    has exited occupies nothing; every other error means the number is unknown,
    which is not the same as small.
    """
    buffer = ctypes.create_string_buffer(_RUSAGE_INFO_V0_BYTES)
    ctypes.set_errno(0)
    if _proc_pid_rusage(pid, _RUSAGE_INFO_V0, ctypes.byref(buffer)) != 0:
        return 0 if ctypes.get_errno() == _ESRCH else None
    return int.from_bytes(
        buffer.raw[_PHYS_FOOTPRINT_OFFSET:_PHYS_FOOTPRINT_OFFSET + 8],
        sys.byteorder)


def _linux_footprint(pid):
    """VmRSS + VmSwap for one pid: bytes, 0 if gone, None if unreadable.

    Linux has no single phys_footprint counter; the sum of the resident set and
    the process's own swap is the closest honest equivalent, and it has the
    property that matters -- it does not fall when pages are swapped out.
    """
    try:
        with open(f"/proc/{pid}/status", "rt") as handle:
            wanted = {"VmRSS:": None, "VmSwap:": None}
            for line in handle:
                name = line.split(maxsplit=1)[0] if line.split() else ""
                if name in wanted:
                    wanted[name] = int(line.split()[1]) * 1024
    except (FileNotFoundError, ProcessLookupError):
        return 0
    except (OSError, ValueError, IndexError):
        return None
    if wanted["VmRSS:"] is None:
        return None                        # kernel thread, or the entry vanished
    return wanted["VmRSS:"] + (wanted["VmSwap:"] or 0)


if sys.platform == "darwin":
    _proc_pid_rusage = ctypes.CDLL(
        "/usr/lib/libSystem.dylib", use_errno=True).proc_pid_rusage
    _proc_pid_rusage.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p)
    _proc_pid_rusage.restype = ctypes.c_int
    MEMORY_METRIC = "darwin:ri_phys_footprint"
    _process_footprint = _darwin_footprint
elif sys.platform.startswith("linux"):
    MEMORY_METRIC = "linux:VmRSS+VmSwap"
    _process_footprint = _linux_footprint
else:
    MEMORY_METRIC = None
    _process_footprint = None


# What every `monotonic_ns` and `elapsed_s` in a witness is measured on since
# 2026-08-27.
#
# `time.monotonic()` was the obvious choice and is the wrong one. On Darwin it
# is `mach_absolute_time`, which does not advance while the system is asleep, so
# a run that spans a lid close is recorded as shorter than it was and its
# wall-time limit is relaxed by exactly that much, silently. Measured on this
# host: a 4151 s Palace run was recorded at 3834.7 s because the machine slept
# for 317 s in the middle of it, and the discrepancy surfaced only because
# Palace's own clock disagreed and a cross-check caught it.
#
# The sleep-inclusive monotonic clock is spelled differently on each platform.
# On Darwin `CLOCK_MONOTONIC` is `mach_continuous_time` and counts sleep, while
# `CLOCK_MONOTONIC_RAW` and `CLOCK_UPTIME_RAW` do not. On Linux the roles
# invert: `CLOCK_MONOTONIC` excludes suspend and `CLOCK_BOOTTIME` includes it.
# Neither name means the same thing on both, so neither is a portable default
# and the choice is made explicitly per platform.
if sys.platform == "darwin":
    WALL_CLOCK = "darwin:CLOCK_MONOTONIC"
    _WALL_CLOCK_ID = time.CLOCK_MONOTONIC
elif sys.platform.startswith("linux"):
    WALL_CLOCK = "linux:CLOCK_BOOTTIME"
    _WALL_CLOCK_ID = getattr(time, "CLOCK_BOOTTIME", None)
else:
    WALL_CLOCK = None
    _WALL_CLOCK_ID = None
if _WALL_CLOCK_ID is None:
    WALL_CLOCK = None


def _monotonic_ns():
    """Nanoseconds on a monotonic clock that keeps counting through sleep."""
    return time.clock_gettime_ns(_WALL_CLOCK_ID)


def require_wall_clock():
    """Refuse to launch where elapsed time could not be measured honestly.

    A precondition rather than a fallback, for the same reason as
    `require_memory_metric`: a run whose wall time is measured on a clock that
    stops is not a bounded run, and the shortfall is invisible in the witness it
    produces.
    """
    if _WALL_CLOCK_ID is None:
        raise WallClockUnavailable(
            f"no sleep-inclusive monotonic clock for platform {sys.platform!r}: "
            "a wall-time bound cannot be enforced, so no bounded run may start "
            "here")


def require_memory_metric():
    """Refuse to launch where the peak-memory bound could not be enforced.

    A precondition, checked before the tree exists rather than discovered at the
    first sample, so an unsupported platform is a launch error and never a run
    that quietly went unbounded.
    """
    if _process_footprint is None:
        raise MemoryMetricUnavailable(
            f"no memory-footprint metric for platform {sys.platform!r}: a peak "
            "memory bound cannot be enforced, so no bounded run may start here")


_TREE_MAY_BE_ALIVE = contextvars.ContextVar(
    "process_monitor_tree_may_be_alive", default=False)


def solver_tree_may_be_alive():
    """True from just before a monitored launch until tree death is confirmed.

    Fail-closed liveness signal for abort decisions: any interrupt delivered
    between launch and confirmed cleanup leaves this True, so callers must
    not rely on exception typing alone to decide the tree is dead.
    """
    return _TREE_MAY_BE_ALIVE.get()


def reset_solver_tree_state():
    return _TREE_MAY_BE_ALIVE.set(False)


def restore_solver_tree_state(token):
    _TREE_MAY_BE_ALIVE.reset(token)


@dataclass(frozen=True)
class ProcessLimits:
    wall_time_s: float
    peak_rss_bytes: int
    output_bytes: int
    refined_panels: int
    gmres_iterations_per_rhs: int


@dataclass(frozen=True)
class ProcessExecution:
    command: tuple[str, ...]
    cwd: str
    returncode: int
    stdout: bytes
    stderr: bytes
    elapsed_s: float
    peak_rss_bytes: int
    output_bytes: int
    directory_growth_bytes: int
    max_refined_panels: int
    max_gmres_iteration: int
    limit_failures: tuple[str, ...]
    limits: ProcessLimits | None = None
    stream_events: tuple[dict, ...] = ()
    monitor_events: tuple[dict, ...] = ()
    resource_samples: tuple[dict, ...] = ()


def _directory_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _reader(stream, source, messages, started_ns):
    byte_offset = 0
    try:
        while True:
            content = stream.read1(65536)
            if not content:
                break
            received_ns = _monotonic_ns() - started_ns
            messages.put((source, byte_offset, received_ns, content))
            byte_offset += len(content)
    finally:
        messages.put((source, byte_offset, _monotonic_ns() - started_ns, None))


def validate_process_event_witness(execution, stdout, stderr):
    if not isinstance(execution, dict):
        raise ValueError("process execution witness must be an object")
    streams = {"stdout": stdout, "stderr": stderr}
    offsets = {"stdout": 0, "stderr": 0}
    last_times = {"stdout": -1, "stderr": -1}
    stream_timestamps = []
    events = execution.get("stream_events")
    if not isinstance(events, list):
        raise ValueError("process stream events must be a list")
    for event in events:
        if (not isinstance(event, dict) or set(event) != {
                "source", "byte_offset", "byte_count", "monotonic_ns", "sha256"
        }):
            raise ValueError("process stream event schema mismatch")
        source = event["source"]
        if source not in streams:
            raise ValueError("process stream event source is invalid")
        offset = event["byte_offset"]
        count = event["byte_count"]
        timestamp = event["monotonic_ns"]
        if (type(offset) is not int or offset != offsets[source]
                or type(count) is not int or count <= 0
                or type(timestamp) is not int or timestamp < 0
                or timestamp < last_times[source]
                or not isinstance(event["sha256"], str)
                or len(event["sha256"]) != 64):
            raise ValueError("process stream event values are invalid")
        content = streams[source][offset:offset + count]
        if (len(content) != count
                or hashlib.sha256(content).hexdigest() != event["sha256"]):
            raise ValueError("process stream event content mismatch")
        offsets[source] += count
        last_times[source] = timestamp
        stream_timestamps.append(timestamp)
    if any(offsets[source] != len(content) for source, content in streams.items()):
        raise ValueError("process stream events do not cover captured bytes")

    monitor_events = execution.get("monitor_events")
    if not isinstance(monitor_events, list) or len(monitor_events) not in (3, 4, 5):
        raise ValueError("process monitor event count is invalid")
    names = []
    timestamps = []
    for event in monitor_events:
        if (not isinstance(event, dict)
                or set(event) != {"event", "monotonic_ns", "detail"}
                or not isinstance(event["event"], str)
                or type(event["monotonic_ns"]) is not int
                or event["monotonic_ns"] < 0
                or event["detail"] is not None
                and not isinstance(event["detail"], str)):
            raise ValueError("process monitor event schema mismatch")
        names.append(event["event"])
        timestamps.append(event["monotonic_ns"])
    failures = execution.get("limit_failures")
    if (not isinstance(failures, list)
            or any(not isinstance(value, str) or not value for value in failures)):
        raise ValueError("process resource-limit failures are invalid")
    expected = ["prelaunch", "process_started", "process_exited"]
    if failures and "kill_initiated" in names:
        expected = [
            "prelaunch", "process_started", "limit_detected",
            "kill_initiated", "process_exited",
        ]
    elif failures:
        expected = [
            "prelaunch", "process_started", "postexit_limit_detected",
            "process_exited",
        ]
    if names != expected or timestamps[0] != 0 or timestamps != sorted(timestamps):
        raise ValueError("process monitor event order is invalid")
    process_started_ns = monitor_events[1]["monotonic_ns"]
    exited_event = next(
        event for event in monitor_events if event["event"] == "process_exited"
    )
    process_exited_ns = exited_event["monotonic_ns"]
    if (type(execution.get("returncode")) is not int
            or exited_event["detail"] != str(execution["returncode"])):
        raise ValueError("process exit detail differs from return code")
    if any(timestamp < process_started_ns or timestamp > process_exited_ns
           for timestamp in stream_timestamps):
        raise ValueError("process stream event lies outside process lifetime")

    samples = execution.get("resource_samples")
    sample_keys = {
        "monotonic_ns", "peak_rss_bytes", "output_bytes",
        "directory_growth_bytes", "max_refined_panels", "max_gmres_iteration",
    }
    if not isinstance(samples, list) or not samples:
        raise ValueError("process resource samples are missing")
    previous = None
    for sample in samples:
        if (not isinstance(sample, dict) or set(sample) != sample_keys
                or any(type(sample[key]) is not int or sample[key] < 0
                       for key in sample_keys)):
            raise ValueError("process resource sample schema mismatch")
        if (sample["monotonic_ns"] < process_started_ns
                or sample["monotonic_ns"] > process_exited_ns):
            raise ValueError("process resource sample lies outside process lifetime")
        if previous is not None and (
                sample["monotonic_ns"] < previous["monotonic_ns"]
                or any(sample[key] < previous[key] for key in sample_keys
                       if key != "monotonic_ns")):
            raise ValueError("process resource samples are not monotonic")
        previous = sample
    expected_final = {
        "peak_rss_bytes": execution.get("peak_rss_bytes"),
        "output_bytes": len(stdout) + len(stderr),
        "directory_growth_bytes": execution.get("directory_growth_bytes"),
        "max_refined_panels": execution.get("max_refined_panels"),
        "max_gmres_iteration": execution.get("max_gmres_iteration"),
    }
    if any(samples[-1][key] != value for key, value in expected_final.items()):
        raise ValueError("process resource summary differs from samples")
    elapsed = execution.get("elapsed_s")
    if type(elapsed) is not float or elapsed != process_exited_ns / 1e9:
        raise ValueError("process elapsed time differs from lifecycle witness")
    limits = execution.get("limits")
    if (not isinstance(limits, dict) or set(limits) != {
            "wall_time_s", "peak_rss_bytes", "output_bytes", "refined_panels",
            "gmres_iterations_per_rhs"}):
        raise ValueError("process resource limits are invalid")
    gmres_matches = re.findall(rb"GMRES Iteration:\s*([^\n\r]+)", stdout + stderr)
    gmres_seen = bool(gmres_matches)
    observed_gmres = max(
        (int(number) for match in gmres_matches
         for number in re.findall(rb"\b\d+\b", match)),
        default=-1,
    )
    if gmres_seen and observed_gmres != expected_final["max_gmres_iteration"]:
        raise ValueError("process GMRES summary differs from stream telemetry")
    derived_failures = []
    checks = (
        (elapsed > limits["wall_time_s"],
         f"wall time exceeded {limits['wall_time_s']:g}s"),
        (expected_final["peak_rss_bytes"] > limits["peak_rss_bytes"],
         f"peak memory footprint exceeded {limits['peak_rss_bytes']} bytes"),
        (expected_final["output_bytes"] + expected_final["directory_growth_bytes"]
         > limits["output_bytes"],
         f"output exceeded {limits['output_bytes']} bytes"),
        (expected_final["max_refined_panels"] > limits["refined_panels"],
         f"refined panels exceeded {limits['refined_panels']}"),
        (gmres_seen and expected_final["max_gmres_iteration"] + 1
         >= limits["gmres_iterations_per_rhs"],
         "GMRES iteration limit reached "
         f"{limits['gmres_iterations_per_rhs']}"),
    )
    derived_failures.extend(message for condition, message in checks if condition)
    if failures != derived_failures:
        raise ValueError("process resource-limit causes differ from samples")
    limit_events = [event for event in monitor_events
                    if event["event"] in {"limit_detected", "postexit_limit_detected"}]
    if failures and (len(limit_events) != 1
                     or limit_events[0]["detail"] != "; ".join(failures)):
        raise ValueError("process resource-limit event detail mismatch")
    return True


def _track_process(member, tracked_descendants):
    try:
        tracked_descendants[member.pid] = member.create_time()
    except (psutil.Error, ProcessLookupError):
        pass


def _identity_process(pid, created):
    try:
        member = psutil.Process(pid)
        if member.create_time() != created:
            return None
        return member
    except (psutil.Error, ProcessLookupError):
        return None


def _discover_descendants(process, leader_created, tracked_descendants):
    roots = []
    leader = _identity_process(process.pid, leader_created)
    if leader is not None:
        roots.append(leader)
    roots.extend(
        member
        for pid, created in tuple(tracked_descendants.items())
        if (member := _identity_process(pid, created)) is not None
    )
    for root in roots:
        try:
            for child in root.children(recursive=True):
                _track_process(child, tracked_descendants)
        except (psutil.Error, ProcessLookupError):
            pass


def _discover_token_processes(tracked_descendants, token, leader_pid):
    for member in psutil.process_iter(("pid", "create_time")):
        try:
            if (member.pid != leader_pid
                    and member.environ().get(MONITOR_TOKEN_ENV) == token):
                _track_process(member, tracked_descendants)
        except (psutil.Error, ProcessLookupError, OSError, SystemError):
            pass


def _tracked_members(tracked_descendants):
    members = []
    for pid, created in tuple(tracked_descendants.items()):
        member = _identity_process(pid, created)
        if member is None:
            tracked_descendants.pop(pid, None)
        else:
            members.append(member)
    return members


def _tree_memory(process, leader_created, tracked_descendants, token):
    """Summed memory footprint of the tracked tree, in bytes.

    Raises MemoryMetricUnavailable rather than skipping a member it cannot read.
    The previous version swallowed those failures and returned a smaller number,
    which is fail-open twice over: it under-reports exactly when the machine is
    least able to answer questions about processes.
    """
    _discover_descendants(process, leader_created, tracked_descendants)
    _discover_token_processes(tracked_descendants, token, process.pid)
    total = 0
    members = _tracked_members(tracked_descendants)
    leader = _identity_process(process.pid, leader_created)
    if leader is not None:
        members.append(leader)
    for member in members:
        value = _process_footprint(member.pid)
        if value is None:
            raise MemoryMetricUnavailable(
                f"memory footprint of pid {member.pid} could not be read")
        total += value
    return total


def _member_alive(member):
    try:
        return member.is_running() and member.status() != psutil.STATUS_ZOMBIE
    except (psutil.Error, ProcessLookupError):
        return False


def _tree_alive(process, tracked_descendants, token):
    if process.poll() is None:
        return True
    if any(map(_member_alive, _tracked_members(tracked_descendants))):
        return True
    _discover_token_processes(tracked_descendants, token, process.pid)
    return any(map(_member_alive, _tracked_members(tracked_descendants)))


def _kill_tree(process, leader_created, tracked_descendants, token):
    _discover_token_processes(tracked_descendants, token, process.pid)
    if _identity_process(process.pid, leader_created) is not None:
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
    members = _tracked_members(tracked_descendants)
    for member in members:
        try:
            member.kill()
        except (psutil.Error, ProcessLookupError):
            pass
    if members:
        psutil.wait_procs(members, timeout=0.1)


def _platform_command(command, *, platform_name=None):
    platform_name = os.name if platform_name is None else platform_name
    command = tuple(str(value) for value in command)
    if platform_name != "nt":
        return command
    runner = Path(__file__).with_name("windows_job_runner.py").resolve()
    return (sys.executable, str(runner), json.dumps(command))


def _cleanup_exceptional_process(
        process, leader_created, tracked_descendants, token, threads):
    try:
        process.kill()
    except (OSError, ProcessLookupError):
        pass
    deadline = _monotonic_ns() + 5_000_000_000
    while _tree_alive(process, tracked_descendants, token):
        _kill_tree(process, leader_created, tracked_descendants, token)
        if _monotonic_ns() >= deadline:
            break
        time.sleep(0.01)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()
    for thread in threads:
        thread.join(timeout=1)
    if _tree_alive(process, tracked_descendants, token):
        raise ProcessCleanupError(
            "process monitor could not establish exceptional cleanup")
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired as error:
        raise ProcessCleanupError(
            "process monitor could not reap exceptional process") from error
    _TREE_MAY_BE_ALIVE.set(False)


def run_monitored_process(
        command, *, cwd, limits, environment=None, additional_output_paths=()):
    """Run a process, killing its tree immediately when a resource gate fails."""
    require_wall_clock()
    require_memory_metric()
    command = tuple(str(value) for value in command)
    launch_command = _platform_command(command)
    cwd = str(Path(cwd).resolve())
    output_roots = (Path(cwd), *(Path(path).resolve()
                                  for path in additional_output_paths))
    if len(set(output_roots)) != len(output_roots):
        raise ValueError("monitored output paths must be distinct")
    for index, root in enumerate(output_roots):
        for other in output_roots[index + 1:]:
            if root in other.parents or other in root.parents:
                raise ValueError("monitored output paths must not overlap")
    started_ns = _monotonic_ns()
    started = started_ns / 1e9
    monitor_events = [{
        "event": "prelaunch",
        "monotonic_ns": 0,
        "detail": None,
    }]
    baseline_size = sum(_directory_size(path) for path in output_roots)
    token = uuid.uuid4().hex
    child_environment = dict(os.environ if environment is None else environment)
    child_environment[MONITOR_TOKEN_ENV] = token
    _TREE_MAY_BE_ALIVE.set(True)
    process = subprocess.Popen(
        launch_command,
        cwd=cwd,
        env=child_environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    tracked_descendants = {}
    leader_created = None
    threads = []
    try:
        monitor_events.append({
            "event": "process_started",
            "monotonic_ns": _monotonic_ns() - started_ns,
            "detail": str(process.pid),
        })
        leader_created = psutil.Process(process.pid).create_time()
        messages = queue.Queue()
        threads = [
            threading.Thread(
                target=_reader,
                args=(stream, source, messages, started_ns),
                daemon=True,
            )
            for source, stream in (
                ("stdout", process.stdout), ("stderr", process.stderr))
        ]
        for thread in threads:
            thread.start()

        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        stream_events = []
        resource_samples = []
        scan_tails = {"stdout": "", "stderr": ""}
        active_readers = len(threads)
        last_disk_check = started
        peak_memory = 0
        directory_growth = 0
        max_panels = 0
        max_gmres = 0
        gmres_seen = False
        failures = []

        while _tree_alive(process, tracked_descendants, token) or active_readers:
            now = _monotonic_ns() / 1e9
            try:
                source, byte_offset, received_ns, content = messages.get(timeout=0.02)
                if content is None:
                    active_readers -= 1
                else:
                    if byte_offset != len(buffers[source]):
                        raise RuntimeError("process stream chunk offset is not contiguous")
                    stream_events.append({
                        "source": source,
                        "byte_offset": byte_offset,
                        "byte_count": len(content),
                        "monotonic_ns": received_ns,
                        "sha256": hashlib.sha256(content).hexdigest(),
                    })
                    buffers[source].extend(content)
                    decoded = content.decode("utf-8", errors="replace")
                    scan_text = scan_tails[source] + decoded
                    panels = re.findall(
                        r"Number of panels after refinement:\s*(\d+)", scan_text,
                        re.IGNORECASE,
                    )
                    if panels:
                        max_panels = max(max_panels, *(int(value) for value in panels))
                    for values in re.findall(
                            r"GMRES Iteration:\s*([^\n\r]+)", scan_text,
                            re.IGNORECASE):
                        numbers = [int(value) for value in re.findall(r"\d+", values)]
                        if numbers:
                            gmres_seen = True
                            max_gmres = max(max_gmres, max(numbers))
                    scan_tails[source] = scan_text[-8192:]
            except queue.Empty:
                pass

            memory = _tree_memory(
                process, leader_created, tracked_descendants, token
            )
            own = _process_footprint(os.getpid())
            if own is None:
                raise MemoryMetricUnavailable(
                    "memory footprint of the monitor itself could not be read")
            peak_memory = max(peak_memory, memory + own)
            output_size = len(buffers["stdout"]) + len(buffers["stderr"])
            if now - last_disk_check >= 0.25:
                directory_growth = max(
                    directory_growth,
                    max(0, sum(_directory_size(path) for path in output_roots)
                        - baseline_size),
                )
                last_disk_check = now

            checks = (
                (now - started > limits.wall_time_s,
                 f"wall time exceeded {limits.wall_time_s:g}s"),
                (peak_memory > limits.peak_rss_bytes,
                 f"peak memory footprint exceeded {limits.peak_rss_bytes} bytes"),
                (output_size + directory_growth > limits.output_bytes,
                 f"output exceeded {limits.output_bytes} bytes"),
                (max_panels > limits.refined_panels,
                 f"refined panels exceeded {limits.refined_panels}"),
                (gmres_seen and max_gmres + 1
                 >= limits.gmres_iterations_per_rhs,
                 "GMRES iteration limit reached "
                 f"{limits.gmres_iterations_per_rhs}"),
            )
            sample = {
                "monotonic_ns": _monotonic_ns() - started_ns,
                "peak_rss_bytes": peak_memory,
                "output_bytes": output_size,
                "directory_growth_bytes": directory_growth,
                "max_refined_panels": max_panels,
                "max_gmres_iteration": max_gmres,
            }
            if not resource_samples or any(
                    sample[key] != resource_samples[-1][key]
                    for key in sample if key != "monotonic_ns"):
                resource_samples.append(sample)

            triggered = [message for condition, message in checks if condition]
            if triggered and not failures:
                failures.extend(triggered)
                monitor_events.append({
                    "event": "limit_detected",
                    "monotonic_ns": _monotonic_ns() - started_ns,
                    "detail": "; ".join(triggered),
                })
                monitor_events.append({
                    "event": "kill_initiated",
                    "monotonic_ns": _monotonic_ns() - started_ns,
                    "detail": None,
                })
            if failures:
                _kill_tree(
                    process, leader_created, tracked_descendants, token,
                )

        for thread in threads:
            thread.join(timeout=1)
        directory_growth = max(
            directory_growth,
            max(0, sum(_directory_size(path) for path in output_roots)
                - baseline_size),
        )
        output_size = len(buffers["stdout"]) + len(buffers["stderr"])
        elapsed_for_checks = (_monotonic_ns() - started_ns) / 1e9
        final_checks = (
            (elapsed_for_checks > limits.wall_time_s,
             f"wall time exceeded {limits.wall_time_s:g}s"),
            (peak_memory > limits.peak_rss_bytes,
             f"peak memory footprint exceeded {limits.peak_rss_bytes} bytes"),
            (output_size + directory_growth > limits.output_bytes,
             f"output exceeded {limits.output_bytes} bytes"),
            (max_panels > limits.refined_panels,
             f"refined panels exceeded {limits.refined_panels}"),
            (gmres_seen and max_gmres + 1
             >= limits.gmres_iterations_per_rhs,
             "GMRES iteration limit reached "
             f"{limits.gmres_iterations_per_rhs}"),
        )
        postexit_failures = []
        for condition, message in final_checks:
            if condition and message not in failures:
                failures.append(message)
                postexit_failures.append(message)
        final_sample = {
            "monotonic_ns": _monotonic_ns() - started_ns,
            "peak_rss_bytes": peak_memory,
            "output_bytes": output_size,
            "directory_growth_bytes": directory_growth,
            "max_refined_panels": max_panels,
            "max_gmres_iteration": max_gmres,
        }
        if not resource_samples or final_sample != resource_samples[-1]:
            resource_samples.append(final_sample)
        if postexit_failures:
            monitor_events.append({
                "event": "postexit_limit_detected",
                "monotonic_ns": _monotonic_ns() - started_ns,
                "detail": "; ".join(postexit_failures),
            })
        exited_ns = _monotonic_ns() - started_ns
        monitor_events.append({
            "event": "process_exited",
            "monotonic_ns": exited_ns,
            "detail": str(process.returncode),
        })
        elapsed = exited_ns / 1e9
        _TREE_MAY_BE_ALIVE.set(False)
        return ProcessExecution(
            command=command,
            cwd=cwd,
            returncode=process.returncode,
            stdout=bytes(buffers["stdout"]),
            stderr=bytes(buffers["stderr"]),
            elapsed_s=elapsed,
            peak_rss_bytes=peak_memory,
            output_bytes=output_size,
            directory_growth_bytes=directory_growth,
            max_refined_panels=max_panels,
            max_gmres_iteration=max_gmres,
            limit_failures=tuple(failures),
            limits=limits,
            stream_events=tuple(stream_events),
            monitor_events=tuple(monitor_events),
            resource_samples=tuple(resource_samples),
        )
    except BaseException:
        try:
            _cleanup_exceptional_process(
                process, leader_created, tracked_descendants, token, threads)
        except BaseException as cleanup_error:
            try:
                survivor = _tree_alive(process, tracked_descendants, token)
            except BaseException:
                survivor = True
            if survivor:
                raise ProcessCleanupError(
                    "process monitor cleanup was interrupted before tree death"
                ) from cleanup_error
            _TREE_MAY_BE_ALIVE.set(False)
            if isinstance(cleanup_error, ProcessCleanupError):
                raise
        raise
