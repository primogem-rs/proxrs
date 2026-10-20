#!/usr/bin/env python3
"""An existing foreground process observes terminal restoration by proxrs."""
import os
import signal
import subprocess
import sys
import termios

signal.signal(signal.SIGINT, lambda signum, frame: None)

with open('/dev/tty', 'rb+', buffering=0) as tty:
    group = os.tcgetpgrp(tty.fileno())
    settings = termios.tcgetattr(tty.fileno())
    process = subprocess.Popen(sys.argv[1:])
    print('PROXRS_PID ' + str(process.pid), file=sys.stderr, flush=True)
    status = process.wait()
    assert os.tcgetpgrp(tty.fileno()) == group, 'foreground ownership was not restored'
    # Darwin sets PENDIN when switching back to canonical input. It is kernel
    # queue state, cleared by the next input operation, not a changed setting:
    # apple-oss-distributions/xnu bsd/kern/tty.c (TIOCSETA and ttypend).
    restored = termios.tcgetattr(tty.fileno())
    assert restored[3] & ~termios.PENDIN == settings[3] & ~termios.PENDIN
    assert restored[:3] + restored[4:] == settings[:3] + settings[4:]
    print('CHECK_RESTORED_INPUT', file=sys.stderr, flush=True)
    assert tty.readline() == b'next-foreground-command\n'
    assert termios.tcgetattr(tty.fileno()) == settings, 'terminal settings were not restored' 
    print('TERMINAL_RESTORED_TO_EXISTING_PROCESS', file=sys.stderr, flush=True)
    sys.exit(status)
