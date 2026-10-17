"""Offline Certbot/process stand-in. Only touches the supplied temporary directory."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


mode, directory = sys.argv[1], Path(sys.argv[2])
directory.mkdir(parents=True, exist_ok=True)
(directory / (mode + '.pid')).write_text(str(os.getpid()), encoding='ascii')

if mode == 'certbot':
    arguments = sys.argv[3:]
    with (directory / 'calls.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(arguments) + '\n')
    if arguments[0] == 'certonly':
        # Existing successfully issued certificate/key bytes must not change.
        for name in ('fullchain.pem', 'privkey.pem'):
            path = directory / name
            if not path.exists():
                path.write_text('fixture-only-' + name, encoding='ascii')
    elif arguments[0] == 'renew':
        assert '--dry-run' in arguments
        if '--no-random-sleep-on-renew' not in arguments:
            (directory / 'random-delay').write_text('368 seconds (scaled to 5 for fixture)', encoding='ascii')
            time.sleep(5)
        if os.environ.get('TEST_CERTBOT_RENEW_FAILURE') == 'true':
            raise SystemExit(23)
        (directory / 'renew-passed').touch()
    else:
        raise SystemExit(24)
else:
    # A launcher -> child -> grandchild reproduces Bash -> Certbot -> helpers.
    # All streams close independently of the launcher, so merely waiting for
    # its pipe EOF cannot prove that the descendants have stopped.
    ignore = mode.startswith('stubborn')
    stop = False

    def terminate(_signal, _frame):
        global stop
        stop = True
        if mode == 'exiting-parent':
            (directory / 'exiting-parent.stopped').touch()
            raise SystemExit(0)

    signal.signal(signal.SIGTERM, signal.SIG_IGN if ignore else terminate)
    child = None
    if mode.endswith('-parent') or mode.endswith('-child'):
        next_mode = ('stubborn-child' if mode == 'exiting-parent' else
                     mode.replace('-parent', '-child') if mode.endswith('-parent') else mode.replace('-child', '-leaf'))
        child = subprocess.Popen([sys.executable, __file__, next_mode, str(directory)],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 5
        while not (directory / (next_mode + '.ready')).exists():
            if time.monotonic() >= deadline:
                raise SystemExit(25)
            time.sleep(.01)
    (directory / (mode + '.ready')).touch()
    while not stop:
        time.sleep(.02)
    if child is not None:
        child.wait(timeout=3)
    (directory / (mode + '.stopped')).touch()
