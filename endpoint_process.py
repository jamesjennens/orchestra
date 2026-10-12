"""Bound a launched endpoint and its native descendants to the service timeout."""
import os
import signal
import subprocess


def stop_group(process):
    if os.name == 'posix':
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
    elif os.name == 'nt' and process.poll() is None:
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       capture_output=True, timeout=15)
    elif process.poll() is None:
        process.kill()


def run(argv, *, input, timeout):
    options = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   text=True, encoding='utf-8')
    if os.name == 'posix':
        options['start_new_session'] = True
    elif os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(argv, **options)
    try:
        stdout, stderr = process.communicate(input=input, timeout=timeout)
    except BaseException:
        # Keep the group identity until cleanup. communicate() may reap the
        # parent while a descendant still owns its output pipes.
        stop_group(process)
        process.communicate()
        raise
    # A native timeout may have stopped only its immediate process before the
    # endpoint returns. No descendant of this request may outlive that return.
    stop_group(process)
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
