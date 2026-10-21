#!/usr/bin/env python3
"""Controlled foreground command; assertions remain in run_cases.py."""
import http.client
import json
import os
import signal
import termios
import time
import ssl
import sys
from urllib.parse import urlsplit


def https(ca):
    proxy = urlsplit(os.environ['HTTPS_PROXY'])
    client = http.client.HTTPSConnection(proxy.hostname, proxy.port, context=ssl.create_default_context(cafile=ca), timeout=8)
    client.set_tunnel('origin.proxrs.test', 8443)
    client.request('GET', '/')
    response = client.getresponse()
    assert response.status == 200
    body = response.read().decode()
    client.close()
    return body


mode, ca, *args = sys.argv[1:]
if mode == 'report':
    print(json.dumps({'args': args, 'env': {name: os.environ.get(name) for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'NO_PROXY', 'no_proxy', 'ALL_PROXY', 'all_proxy')},
                      'pid': os.getpid(), 'pgid': os.getpgrp(), 'stdin': sys.stdin.read(), 'https': https(ca)}), flush=True)
    print('controlled-child-stderr', file=sys.stderr, flush=True)
    sys.exit(23)

elif mode == 'interactive':
    def interrupt(signum, frame):
        assert https(ca) == 'proxrs-docker-origin\n'
        print('CHILD_HANDLED_INT_AND_HTTPS', file=sys.stderr, flush=True)
    signal.signal(signal.SIGINT, interrupt)
    print('CHILD_READY ' + json.dumps({'pid': os.getpid(), 'pgid': os.getpgrp(), 'foreground': os.tcgetpgrp(0), 'tty': os.isatty(0), 'blocking': os.get_blocking(0)}), file=sys.stderr, flush=True)
    entered = sys.stdin.readline()
    print(json.dumps({'entered': entered, 'https': https(ca)}), flush=True)
    settings = termios.tcgetattr(0)
    settings[3] &= ~(termios.ECHO | termios.ICANON)
    termios.tcsetattr(0, termios.TCSANOW, settings)
    sys.exit(7)

elif mode == 'descendants':
    stubborn = args[0] == 'stubborn'
    if stubborn:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    ready_read, ready_write = os.pipe()
    child = os.fork()
    if child == 0:
        os.close(ready_read)
        os.write(ready_write, b'ready')
        os.close(ready_write)
        while True:
            time.sleep(1)
    os.close(ready_write)
    assert os.read(ready_read, 5) == b'ready'
    os.close(ready_read)
    if args[1] == 'signal_exit':
        signal.signal(signal.SIGUSR1, lambda signum, frame: sys.exit(29))
    print(json.dumps({'pid': os.getpid(), 'descendant': child, 'proxy': os.environ['HTTP_PROXY']}), flush=True)
    if args[1] == 'exit':
        sys.exit(17)
    while True:
        time.sleep(1)

elif mode == 'recovery_interactive':
    signal.signal(signal.SIGINT, lambda signum, frame: print('CHILD_HANDLED_INT_DURING_RETRY', file=sys.stderr, flush=True))
    print('CHILD_READY ' + json.dumps({'pid': os.getpid(), 'pgid': os.getpgrp(), 'foreground': os.tcgetpgrp(0), 'proxy': os.environ['HTTP_PROXY']}), file=sys.stderr, flush=True)
    for entered in sys.stdin:
        if entered.strip() == 'probe':
            assert https(ca) == 'proxrs-docker-origin\n'
            print('CHILD_HTTPS_AFTER_RECOVERY', file=sys.stderr, flush=True)
        elif entered.strip() == 'exit':
            sys.exit(19)
