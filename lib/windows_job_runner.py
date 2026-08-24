#!/usr/bin/env python3
"""Launch one command suspended inside a kill-on-close Windows Job Object."""
import json
import subprocess
import sys
import time


def _assign_resume_wait(job, process, thread, *, win32api, win32event,
                        win32job, win32process, sleep=time.sleep):
    assigned = False
    try:
        win32job.AssignProcessToJobObject(job, process)
        assigned = True
        win32process.ResumeThread(thread)
        win32event.WaitForSingleObject(process, win32event.INFINITE)
        exit_code = win32process.GetExitCodeProcess(process)
        while win32job.QueryInformationJobObject(
                job, win32job.JobObjectBasicAccountingInformation
        )["ActiveProcesses"]:
            sleep(0.02)
        return exit_code
    finally:
        if not assigned:
            try:
                win32api.TerminateProcess(process, 1)
            finally:
                win32event.WaitForSingleObject(process, win32event.INFINITE)


def main(argv=None):
    if sys.platform != "win32":
        raise SystemExit("windows_job_runner requires Windows")
    import win32api
    import win32con
    import win32event
    import win32job
    import win32process

    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        raise SystemExit("usage: windows_job_runner.py JSON_COMMAND")
    command = json.loads(arguments[0])
    if not isinstance(command, list) or not command:
        raise SystemExit("JSON_COMMAND must be a non-empty string list")
    if any(not isinstance(value, str) for value in command):
        raise SystemExit("JSON_COMMAND values must be strings")

    job = win32job.CreateJobObject(None, "")
    information = win32job.QueryInformationJobObject(
        job, win32job.JobObjectExtendedLimitInformation
    )
    information["BasicLimitInformation"]["LimitFlags"] |= (
        win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    )
    win32job.SetInformationJobObject(
        job,
        win32job.JobObjectExtendedLimitInformation,
        information,
    )
    startup = win32process.STARTUPINFO()
    startup.dwFlags |= win32con.STARTF_USESTDHANDLES
    startup.hStdInput = win32api.GetStdHandle(win32api.STD_INPUT_HANDLE)
    startup.hStdOutput = win32api.GetStdHandle(win32api.STD_OUTPUT_HANDLE)
    startup.hStdError = win32api.GetStdHandle(win32api.STD_ERROR_HANDLE)
    process, thread, _, _ = win32process.CreateProcess(
        None,
        subprocess.list2cmdline(command),
        None,
        None,
        True,
        win32process.CREATE_SUSPENDED,
        None,
        None,
        startup,
    )
    try:
        return _assign_resume_wait(
            job,
            process,
            thread,
            win32api=win32api,
            win32event=win32event,
            win32job=win32job,
            win32process=win32process,
        )
    finally:
        win32api.CloseHandle(thread)
        win32api.CloseHandle(process)
        win32api.CloseHandle(job)


if __name__ == "__main__":
    raise SystemExit(main())
