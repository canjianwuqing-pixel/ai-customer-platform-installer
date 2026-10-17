"""Bounded host commands: terminate their whole POSIX session before rollback."""
import os
from pathlib import Path
import signal
import subprocess
import time


TERMINATE_GRACE_SECONDS = 3.0


def _signal_group(process, sig):
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


def _group_exists(process):
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    return True


def _group_running(process):
    if not _group_exists(process):
        return False
    try:
        pids = os.listdir('/proc')
    except OSError:
        return True
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            if os.getpgid(int(pid)) != process.pid:
                continue
            state = (Path('/proc') / pid / 'stat').read_text().rsplit(')', 1)[1].split()[0]
        except (ProcessLookupError, FileNotFoundError):
            continue
        except (OSError, ValueError, IndexError):
            return True  # Cannot prove the group is stopped: do not roll back.
        if state not in {'Z', 'X'}:
            return True
    return False


def _terminate_and_reap(process):
    _signal_group(process, signal.SIGTERM)
    deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
    while _group_exists(process):
        # Reap the direct child too, but its exit/pipe EOF does NOT prove its
        # Certbot descendants have exited (some may ignore TERM).
        process.poll()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _signal_group(process, signal.SIGKILL)
            break
        time.sleep(min(.05, remaining))
    # Drain/close pipes and reap the launcher before the caller can roll back.
    # Never expose timeout output: it can contain commands/nonces/credentials.
    process.communicate()
    # SIGKILL delivery is asynchronous. Wait for actual termination, not just
    # successful signal delivery. Orphan zombies await init but cannot execute.
    # Ubuntu provides /proc; do not proceed to rollback if cleanup is unproven.
    while _group_running(process):
        _signal_group(process, signal.SIGKILL)
        time.sleep(.01)


def run_site_command(args, *, input=None, timeout=None, **options):
    if os.name != 'posix':
        # Development-only compatibility. Host deployment already requires
        # POSIX (fcntl/systemd); real process-tree tests must run on POSIX.
        return subprocess.run(args, input=input, timeout=timeout, **options)
    if input is not None:
        if 'stdin' in options:
            raise ValueError('stdin and input cannot be combined')
        options['stdin'] = subprocess.PIPE
    else:
        options.setdefault('stdin', subprocess.DEVNULL)
    with subprocess.Popen(args, start_new_session=True, **options) as process:
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except BaseException:
            _terminate_and_reap(process)
            raise
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
