#!/usr/bin/env python3
"""Bounded subprocess execution with byte-exact stream capture."""
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
            received_ns = time.monotonic_ns() - started_ns
            messages.put((source, byte_offset, received_ns, content))
            byte_offset += len(content)
    finally:
        messages.put((source, byte_offset, time.monotonic_ns() - started_ns, None))


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
         f"peak RSS exceeded {limits['peak_rss_bytes']} bytes"),
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


def _tree_rss(process, leader_created, tracked_descendants, token):
    _discover_descendants(process, leader_created, tracked_descendants)
    _discover_token_processes(tracked_descendants, token, process.pid)
    total = 0
    members = _tracked_members(tracked_descendants)
    leader = _identity_process(process.pid, leader_created)
    if leader is not None:
        members.append(leader)
    for member in members:
        try:
            total += member.memory_info().rss
        except (psutil.Error, ProcessLookupError):
            pass
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


def run_monitored_process(
        command, *, cwd, limits, environment=None, additional_output_paths=()):
    """Run a process, killing its tree immediately when a resource gate fails."""
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
    started_ns = time.monotonic_ns()
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
            "monotonic_ns": time.monotonic_ns() - started_ns,
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
        peak_rss = 0
        directory_growth = 0
        max_panels = 0
        max_gmres = 0
        gmres_seen = False
        failures = []
        monitor_process = psutil.Process(os.getpid())

        while _tree_alive(process, tracked_descendants, token) or active_readers:
            now = time.monotonic()
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

            rss = _tree_rss(
                process, leader_created, tracked_descendants, token
            )
            try:
                rss += monitor_process.memory_info().rss
            except (psutil.Error, ProcessLookupError):
                pass
            peak_rss = max(peak_rss, rss)
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
                (peak_rss > limits.peak_rss_bytes,
                 f"peak RSS exceeded {limits.peak_rss_bytes} bytes"),
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
                "monotonic_ns": time.monotonic_ns() - started_ns,
                "peak_rss_bytes": peak_rss,
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
                    "monotonic_ns": time.monotonic_ns() - started_ns,
                    "detail": "; ".join(triggered),
                })
                monitor_events.append({
                    "event": "kill_initiated",
                    "monotonic_ns": time.monotonic_ns() - started_ns,
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
        elapsed_for_checks = (time.monotonic_ns() - started_ns) / 1e9
        final_checks = (
            (elapsed_for_checks > limits.wall_time_s,
             f"wall time exceeded {limits.wall_time_s:g}s"),
            (peak_rss > limits.peak_rss_bytes,
             f"peak RSS exceeded {limits.peak_rss_bytes} bytes"),
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
            "monotonic_ns": time.monotonic_ns() - started_ns,
            "peak_rss_bytes": peak_rss,
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
                "monotonic_ns": time.monotonic_ns() - started_ns,
                "detail": "; ".join(postexit_failures),
            })
        exited_ns = time.monotonic_ns() - started_ns
        monitor_events.append({
            "event": "process_exited",
            "monotonic_ns": exited_ns,
            "detail": str(process.returncode),
        })
        elapsed = exited_ns / 1e9
        return ProcessExecution(
            command=command,
            cwd=cwd,
            returncode=process.returncode,
            stdout=bytes(buffers["stdout"]),
            stderr=bytes(buffers["stderr"]),
            elapsed_s=elapsed,
            peak_rss_bytes=peak_rss,
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
    except BaseException as error:
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
        deadline = time.monotonic() + 5.0
        while _tree_alive(process, tracked_descendants, token):
            _kill_tree(process, leader_created, tracked_descendants, token)
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        for thread in threads:
            thread.join(timeout=1)
        if _tree_alive(process, tracked_descendants, token):
            raise ProcessCleanupError(
                "process monitor could not establish exceptional cleanup"
            ) from error
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired as cleanup_error:
            raise ProcessCleanupError(
                "process monitor could not reap exceptional process"
            ) from cleanup_error
        raise
