#!/usr/bin/env python3
"""Real SSH failure injection through a bounded, opaque loopback TCP relay."""
import argparse
import contextlib
import http.client
import json
import os
import queue
import re
import select
import socket
import signal
import subprocess
import sys
import threading
import time

from urllib.parse import urlsplit

from proxy_cases import Fixture, ROOT


class SSHRelay:
    def __init__(self, fixture, mode='forward'):
        self.fixture = fixture
        self.target = fixture.port
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(8)
        self.listener.setblocking(False)
        self.port = self.listener.getsockname()[1]
        self.wake, self.control = socket.socketpair()
        self.commands = queue.Queue(8)
        self.mode = mode
        self.attempts = []
        self.error = None
        self.pairs = []
        self.thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self):
        self.original_trust = (self.fixture.path / 'known_hosts').read_text()
        (self.fixture.path / 'known_hosts').write_text(f'[127.0.0.1]:{self.port} {self.fixture.key}\n')
        self.fixture.port = self.port
        self.thread.start()
        return self

    def change(self, action):
        ack = threading.Event()
        self.commands.put((action, ack), timeout=2)
        self.control.sendall(b'x')
        assert ack.wait(3), 'relay control did not complete'
        assert self.error is None, self.error

    def _close(self, pair):
        if pair in self.pairs:
            self.pairs.remove(pair)
        for stream in pair['sockets']:
            with contextlib.suppress(OSError):
                stream.shutdown(socket.SHUT_RDWR)
            stream.close()

    def _run(self):
        try:
            running = True
            while running:
                reads = [self.wake, self.listener]
                writes = []
                owners = {}
                for pair in self.pairs:
                    for side, stream in enumerate(pair['sockets']):
                        owners[stream] = (pair, side)
                        if not pair['eof'][side] and (pair['muted'] or len(pair['buffers'][1-side]) < 16384):
                            reads.append(stream)
                        if pair['buffers'][side]:
                            writes.append(stream)
                readable, writable, _ = select.select(reads, writes, [], 1)
                if self.wake in readable:
                    self.wake.recv(8)
                    while not self.commands.empty():
                        action, ack = self.commands.get_nowait()
                        if action == 'stop':
                            running = False
                        elif action == 'silence':
                            self.mode = 'reject'
                            for pair in self.pairs:
                                pair['muted'] = True
                                pair['buffers'] = [bytearray(), bytearray()]
                        else:
                            self.mode = action
                            if action != 'forward':
                                for pair in self.pairs[:]:
                                    self._close(pair)
                        ack.set()
                    continue
                if self.listener in readable:
                    stream, _ = self.listener.accept()
                    self.attempts.append(time.monotonic())
                    assert len(self.attempts) <= 64, 'unexpected unbounded retries'
                    if self.mode == 'reject' or len(self.pairs) >= 8:
                        stream.close()
                    else:
                        remote = socket.create_connection(('127.0.0.1', self.target), timeout=2)
                        stream.setblocking(False)
                        remote.setblocking(False)
                        self.pairs.append({'sockets': [stream, remote], 'buffers': [bytearray(), bytearray()],
                                           'eof': [False, False], 'shutdown': [False, False], 'muted': self.mode == 'stall'})
                for stream in writable + readable:
                    if stream not in owners:
                        continue
                    pair, side = owners[stream]
                    if pair not in self.pairs:
                        continue
                    try:
                        if stream in writable and pair['buffers'][side]:
                            sent = stream.send(pair['buffers'][side])
                            del pair['buffers'][side][:sent]
                        if stream in readable:
                            data = stream.recv(16384 if pair['muted'] else 16384-len(pair['buffers'][1-side]))
                            if not data:
                                pair['eof'][side] = True
                            elif not pair['muted']:
                                pair['buffers'][1-side].extend(data)
                        for dest in (0, 1):
                            if pair['eof'][1-dest] and not pair['buffers'][dest] and not pair['shutdown'][dest]:
                                pair['sockets'][dest].shutdown(socket.SHUT_WR)
                                pair['shutdown'][dest] = True
                        if all(pair['shutdown']):
                            self._close(pair)
                    except BlockingIOError:
                        pass
                    except (ConnectionError, OSError):
                        self._close(pair)
        except BaseException as error:
            self.error = repr(error)
        finally:
            for pair in self.pairs[:]:
                self._close(pair)

    def __exit__(self, *args):
        try:
            self.change('stop')
        finally:
            self.thread.join(4)
            self.listener.close()
            self.wake.close()
            self.control.close()
            self.fixture.port = self.target
            (self.fixture.path / 'known_hosts').write_text(self.original_trust)
        assert not self.thread.is_alive(), 'relay thread survived cleanup'
        assert self.error is None, self.error


def wait_for(predicate, timeout=5, description='condition'):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(.02)
    raise AssertionError('timed out waiting for ' + description)


def await_ready(fixture, process, out, err, timeout=8):
    def ready():
        out.seek(0)
        match = re.search(r"export HTTP_PROXY='http://127.0.0.1:(\d+)'", out.read())
        if match:
            fixture.proxy_port = int(match[1])
        if process.poll() is not None:
            err.seek(0)
            raise AssertionError('exited before readiness: ' + err.read())
        return match
    wait_for(ready, timeout, 'retried readiness')


def startup_transient(fixture):
    with SSHRelay(fixture, 'reject') as relay:
        process, out, err = fixture.start()
        wait_for(lambda: len(relay.attempts) == 1, description='first failed SSH attempt')
        time.sleep(.15)
        assert process.poll() is None, 'transient startup failure was not retried'
        out.seek(0)
        assert not out.read(), 'startup failure printed readiness'
        relay.change('forward')
        await_ready(fixture, process, out, err)
        fixture.https()
        assert len(relay.attempts) == 2, relay.attempts
        assert .85 <= relay.attempts[1]-relay.attempts[0] <= 2, relay.attempts
        process.terminate()
        assert process.wait(5) == 0
    print('PASS: transient startup retries after one second, then real TLS succeeds')


def available(fixture):
    try:
        stream = socket.create_connection(('127.0.0.1', fixture.proxy_port), timeout=1)
        with stream:
            stream.sendall(b'GET http://origin.proxrs.test:8080/ HTTP/1.1\r\nHost: origin.proxrs.test\r\nConnection: close\r\n\r\n')
            return stream.recv(1024).startswith(b'HTTP/1.1 200 ')
    except OSError:
        return False


def established_recovery(fixture):
    with SSHRelay(fixture) as relay:
        process = fixture.ready()
        port = fixture.proxy_port
        reused = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
        reused.request('GET', 'http://origin.proxrs.test:8080/')
        assert reused.getresponse().read() == b'proxrs-docker-origin\n'
        original_socket = reused.sock
        with fixture.connect('127.0.0.1:9000') as old:
            old.sendall(b'before-outage')
            assert old.recv(100) == b'before-outage'
            relay.change('reject')
            old.settimeout(2)
            try:
                assert old.recv(100) == b'', 'failed stream injected new HTTP data'
            except ConnectionResetError:
                pass
            time.sleep(.15)
            assert process.poll() is None, 'established loss ended the session instead of recovering'
            start = time.monotonic()
            with fixture.connect(status=503):
                pass
            assert time.monotonic()-start < 1, 'unavailable request waited for reconnect'
            relay.change('forward')
            wait_for(lambda: available(fixture), 5, 'healthy request after reconnect')
            assert fixture.proxy_port == port
            fixture.https()
            reused.request('GET', 'http://origin.proxrs.test:8080/')
            assert reused.getresponse().read() == b'proxrs-docker-origin\n'
            assert reused.sock is original_socket, 'client connection was not reused'
            reused.close()
            process.terminate()
            assert process.wait(5) == 0
    print('PASS: old stream fails, same listener returns prompt 503, new SSH serves HTTPS')


def authentication_disconnect(fixture):
    from trust_cases import observed_authentication, authentication_count
    with observed_authentication(fixture), SSHRelay(fixture) as relay:
        pam = fixture.docker('cat', '/etc/pam.d/sshd')
        first, rest = pam.split(b'\n', 1)
        try:
            fixture.docker('sh', '-c', 'cat > /etc/pam.d/sshd', input=first+b'\nauth optional pam_exec.so /bin/sleep 30\n'+rest)
            before = authentication_count(fixture)
            process, out, err = fixture.start()
            wait_for(lambda: authentication_count(fixture) > before, 8, 'first real password attempt')
            fixture.docker('sh', '-c', 'cat > /etc/pam.d/sshd', input=pam)
            relay.change('reject')
            time.sleep(.15)
            assert process.poll() is None, 'transport drop during authentication was treated as password rejection'
            relay.change('forward')
            await_ready(fixture, process, out, err)
            fixture.https()
            assert authentication_count(fixture) == before+2
            process.terminate()
            assert process.wait(5) == 0
        finally:
            fixture.docker('sh', '-c', 'cat > /etc/pam.d/sshd', input=pam)
    print('PASS: authentication transport loss retries, retained password succeeds without another input line')


def assert_retry_gaps(attempts, expected):
    gaps = [later-earlier for earlier, later in zip(attempts, attempts[1:])]
    assert len(gaps) == len(expected), (gaps, expected)
    for gap, minimum in zip(gaps, expected):
        assert minimum-.15 <= gap <= minimum+1.5, (gaps, expected)


def bounded_startup(fixture):
    for mode, timeout, expected in [('reject', 12, [1, 2, 4]), ('stall', 52, [11, 12, 14])]:
        with SSHRelay(fixture, mode) as relay:
            start = time.monotonic()
            process, out, err = fixture.start()
            assert process.wait(timeout) == 1
            elapsed = time.monotonic()-start
            assert len(relay.attempts) == 4, relay.attempts
            assert_retry_gaps(relay.attempts, expected)
            assert (6.7 if mode == 'reject' else 46.7) <= elapsed <= timeout, elapsed
            out.seek(0)
            err.seek(0)
            assert not out.read(), 'failed startup printed readiness'
            diagnostic = err.read()
            assert 'retry budget exhausted' in diagnostic
            if mode == 'stall':
                assert 'startup deadline' in diagnostic
            print(f'PASS: startup {mode} uses initial +3 attempts, elapsed={elapsed:.2f}s, gaps=' + str([round(b-a, 2) for a,b in zip(relay.attempts, relay.attempts[1:])]))


def flapping_and_stability(fixture):
    with SSHRelay(fixture) as relay:
        process = fixture.ready()
        for expected in (1, 2):
            start = time.monotonic()
            relay.change('reject')
            wait_for(lambda: not available(fixture), 2, '503 during outage')
            relay.change('forward')
            wait_for(lambda: available(fixture), expected+3, 'recovery after flap')
            assert expected-.1 <= time.monotonic()-start <= expected+2
        with fixture.connect('127.0.0.1:9000') as quiet:
            time.sleep(61)
            quiet.sendall(b'quiet-but-healthy')
            assert quiet.recv(100) == b'quiet-but-healthy', 'healthy quiet tunnel expired'
        # More than 60 stable seconds restores all three retry slots.
        for expected in (1, 2, 4):
            start = time.monotonic()
            relay.change('reject')
            wait_for(lambda: not available(fixture), 2, '503 during outage')
            relay.change('forward')
            wait_for(lambda: available(fixture), expected+3, 'recovery after stable reset')
            elapsed = time.monotonic()-start
            assert expected-.1 <= elapsed <= expected+2, (expected, elapsed)
        attempts = len(relay.attempts)
        start = time.monotonic()
        relay.change('reject')
        assert process.wait(3) == 1, 'flapping did not exhaust the shared budget'
        assert len(relay.attempts) == attempts, 'exhausted budget tried another SSH connection'
        assert time.monotonic()-start < 2
    print('PASS: flaps share 1/2/4 budget; 61s healthy quiet tunnel survives and restores the full budget')


def silent_loss(fixture):
    with SSHRelay(fixture) as relay:
        process = fixture.ready()
        with fixture.connect('127.0.0.1:9000') as old:
            old.sendall(b'last-healthy-packet')
            assert old.recv(100) == b'last-healthy-packet'
            start = time.monotonic()
            relay.change('silence')
            # No application request or TCP EOF assists the liveness detector.
            old.settimeout(68)
            assert old.recv(100) == b'', 'silent loss did not close the old tunnel'
            elapsed = time.monotonic()-start
            assert 54 <= elapsed <= 66, elapsed
            assert process.poll() is None
            with fixture.connect(status=503):
                pass
            relay.change('forward')
            wait_for(lambda: available(fixture), 8, 'recovery after silent transport')
            fixture.https()
            process.terminate()
            assert process.wait(5) == 0
    print(f'PASS: silent SSH transport detected after three unanswered probes in {elapsed:.2f}s; recovery succeeds')


def startup_cancellation(fixture):
    for mode in ('reject', 'stall'):
        for stop in (signal.SIGINT, signal.SIGTERM):
            with SSHRelay(fixture, mode) as relay:
                process, out, err = fixture.start()
                wait_for(lambda: len(relay.attempts) == 1, description='startup attempt')
                time.sleep(.2)
                start = time.monotonic()
                process.send_signal(stop)
                assert process.wait(2) == 0
                assert time.monotonic()-start < 1
                assert len(relay.attempts) == 1
    print('PASS: directed INT/TERM cancel startup retry delays and stalled handshakes promptly')


def counted_origin(fixture):
    return int(fixture.docker('python3', '-c', "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8080/counted').read().decode())"))


def no_replay(fixture):
    with SSHRelay(fixture) as relay:
        process = fixture.ready()
        before = counted_origin(fixture)
        client = http.client.HTTPConnection('127.0.0.1', fixture.proxy_port, timeout=3)
        client.request('POST', 'http://origin.proxrs.test:8080/counted', body=b'proxrs-sensitive-body-marker',
                       headers={'Authorization': 'Bearer proxrs-sensitive-header-marker'})
        response = client.getresponse()
        assert response.status == 200 and response.read(5) == b'first'
        assert counted_origin(fixture) == before+1
        relay.change('reject')
        try:
            extra = response.read()
        except http.client.IncompleteRead as error:
            extra = error.partial
        assert b'HTTP/' not in extra, 'failed response injected a second HTTP status'
        client.close()
        with fixture.connect(status=503):
            pass
        unavailable = http.client.HTTPConnection('127.0.0.1', fixture.proxy_port, timeout=1)
        unavailable.request('GET', 'http://origin.proxrs.test:8080/')
        assert unavailable.getresponse().status == 503
        unavailable.close()
        assert counted_origin(fixture) == before+1
        relay.change('forward')
        wait_for(lambda: available(fixture), 5, 'new request after interrupted POST')
        fixture.https()
        assert counted_origin(fixture) == before+1, 'interrupted POST was replayed'
        process.terminate()
        assert process.wait(5) == 0
        _, out, err = fixture.processes[-1]
        out.seek(0)
        err.seek(0)
        diagnostic = out.read()+err.read()
        assert 'proxrs-sensitive-body-marker' not in diagnostic and 'proxrs-sensitive-header-marker' not in diagnostic
    print('PASS: origin side-effect counter proves no replay; broken HTTP response closes without a second status')


def terminal_failures(fixture):
    from trust_cases import observed_authentication, authentication_count
    with observed_authentication(fixture):
        config = fixture.docker('cat', '/etc/ssh/sshd_config')
        try:
            # The server closes after an explicit rejection. It must remain
            # terminal even if the SSH driver is closed when the result arrives.
            fixture.docker('sh', '-c', 'cat > /etc/ssh/sshd_config', input=config+b'\nMaxAuthTries 1\n')
            fixture.docker('kill', '-HUP', '1')
            with SSHRelay(fixture) as relay:
                before = authentication_count(fixture)
                process, out, err = fixture.start(password='wrong-disposable-password')
                assert process.wait(8) == 1
                assert authentication_count(fixture) == before+1
                assert len(relay.attempts) == 1
                err.seek(0)
                diagnostic = err.read()
                assert 'SSH password authentication failed' in diagnostic or 'authentication rejected' in diagnostic, diagnostic
        finally:
            fixture.docker('sh', '-c', 'cat > /etc/ssh/sshd_config', input=config)
            fixture.docker('kill', '-HUP', '1')
        for mode in ('changed', 'revoked', 'untrusted', 'unreadable'):
            with SSHRelay(fixture) as relay:
                process = fixture.ready()
                before = authentication_count(fixture)
                known = fixture.path / 'known_hosts'
                trusted = known.read_text()
                try:
                    if mode == 'changed':
                        fixture.docker('ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', '/run/recovery_changed_key')
                        fixture.docker('sh', '-c', 'cat > /etc/ssh/sshd_config', input=config.replace(b'/etc/ssh/ssh_host_ed25519_key', b'/run/recovery_changed_key'))
                        fixture.docker('kill', '-HUP', '1')
                    elif mode == 'revoked':
                        known.write_text('@revoked ' + trusted)
                    elif mode == 'untrusted':
                        known.write_text('')
                    else:
                        known.chmod(0)
                    relay.change('reject')
                    wait_for(lambda: not available(fixture), 2, 'lost SSH before trust recheck')
                    relay.change('forward')
                    assert process.wait(5) == 1
                    assert len(relay.attempts) == 2, 'terminal trust failure retried again'
                    assert authentication_count(fixture) == before, 'untrusted reconnect sent password'
                finally:
                    known.chmod(0o600)
                    known.write_text(trusted)
                    if mode == 'changed':
                        fixture.docker('sh', '-c', 'cat > /etc/ssh/sshd_config', input=config)
                        fixture.docker('kill', '-HUP', '1')
    print('PASS: explicit auth rejection, changed/revoked/untrusted/unreadable reconnect trust stop without another password attempt')



def managed_recovery(fixture):
    from run_cases import run_args, alive
    from trust_cases import TerminalProcess
    for finish in ('recover', 'exhaust'):
        with SSHRelay(fixture) as relay:
            args = run_args(fixture, ['recovery_interactive', str(fixture.path / 'ca.pem')])
            wrapped = [sys.executable, str(ROOT / 'tests/docker/managed_terminal.py'), *args]
            child = None
            with TerminalProcess(fixture, wrapped, password_pipe=True) as process:
                try:
                    output = process.wait_text('CHILD_READY ')
                    child = json.loads(re.search(r'CHILD_READY (.+)', output)[1])
                    fixture.proxy_port = urlsplit(child['proxy']).port
                    assert child['pid'] == child['pgid'] == child['foreground'], child
                    relay.change('reject')
                    wait_for(lambda: not available(fixture), 2, 'managed outage')
                    assert alive(child['pid']) and process.poll() is None
                    process.send('\x03')
                    process.wait_text('CHILD_HANDLED_INT_DURING_RETRY')
                    assert alive(child['pid']) and process.poll() is None
                    if finish == 'recover':
                        relay.change('forward')
                        wait_for(lambda: available(fixture), 5, 'managed recovery')
                        process.send('probe\n')
                        process.wait_text('CHILD_HTTPS_AFTER_RECOVERY')
                        assert alive(child['pid'])
                        process.send('exit\n')
                    process.wait_text('CHECK_RESTORED_INPUT', 15)
                    process.send('next-foreground-command\n')
                    assert process.wait() == (19 if finish == 'recover' else 1), process.read(0)
                    assert 'TERMINAL_RESTORED_TO_EXISTING_PROCESS' in process.read(0)
                    process.restored()
                    assert not alive(child['pid'])
                    with socket.socket() as probe:
                        assert probe.connect_ex(('127.0.0.1', fixture.proxy_port)) != 0
                    assert len(relay.attempts) == (2 if finish == 'recover' else 4)
                finally:
                    if child:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(child['pid'], signal.SIGKILL)
    print('PASS: same foreground child handles Ctrl-C during retries, uses recovered HTTPS, and terminal restores on exit/exhaustion')


def managed_cancellation(fixture):
    from run_cases import run_args, alive
    sentinel = subprocess.Popen(['sleep', '90'], start_new_session=True)
    try:
        for mode, finish in (('reject', 'child_exit'), ('reject', 'terminate'), ('stall', 'interrupt')):
            with SSHRelay(fixture) as relay:
                process, out, err = fixture.start(run_args(fixture, ['descendants', str(fixture.path / 'ca.pem'), 'cooperative', 'signal_exit']))
                child = None
                try:
                    def child_ready():
                        out.seek(0)
                        line = out.readline()
                        return json.loads(line) if line else None
                    child = wait_for(child_ready, 8, 'managed child and descendant')
                    fixture.proxy_port = urlsplit(child['proxy']).port
                    relay.change(mode)
                    wait_for(lambda: not available(fixture), 2, 'managed outage')
                    if mode == 'stall':
                        wait_for(lambda: len(relay.attempts) == 2, 3, 'stalled reconnect handshake')
                    start = time.monotonic()
                    if finish == 'child_exit':
                        os.kill(child['pid'], signal.SIGUSR1)
                    else:
                        process.send_signal(signal.SIGTERM if finish == 'terminate' else signal.SIGINT)
                    assert process.wait(3) == (29 if finish == 'child_exit' else 0)
                    assert time.monotonic()-start < 2
                    assert not alive(child['pid']) and not alive(child['descendant'])
                    with socket.socket() as probe:
                        assert probe.connect_ex(('127.0.0.1', fixture.proxy_port)) != 0
                    attempts = len(relay.attempts)
                    time.sleep(1.1)
                    assert len(relay.attempts) == attempts, 'canceled retry attempted another connection'
                    assert sentinel.poll() is None, 'unrelated process was signaled'
                finally:
                    if child:
                        with contextlib.suppress(ProcessLookupError):
                            os.killpg(child['pid'], signal.SIGKILL)
    finally:
        sentinel.terminate()
        sentinel.wait()
    print('PASS: child exit and directed signals cancel retry delay/handshake, preserve status, and clean only owned processes')


def managed_startup(fixture):
    from run_cases import run_args
    with SSHRelay(fixture, 'reject') as relay:
        process, out, err = fixture.start(run_args(fixture, ['report', str(fixture.path / 'ca.pem')]))
        wait_for(lambda: len(relay.attempts) == 1, description='failed managed startup')
        time.sleep(.15)
        out.seek(0)
        assert not out.read() and process.poll() is None, 'managed child started before readiness'
        relay.change('forward')
        assert process.wait(8) == 23
        out.seek(0)
        child = json.loads(out.read())
        assert child['https'] == 'proxrs-docker-origin\n' and child['stdin'] == ''
        assert len(relay.attempts) == 2
    print('PASS: managed command waits through startup outage, launches once after authentication, and returns child status')



def startup_budget_carries_forward(fixture):
    with SSHRelay(fixture, 'reject') as relay:
        process, out, err = fixture.start()
        wait_for(lambda: len(relay.attempts) == 1, description='startup retry slot')
        relay.change('forward')
        await_ready(fixture, process, out, err)
        assert len(relay.attempts) == 2
        start = time.monotonic()
        relay.change('reject')
        assert process.wait(9) == 1
        elapsed = time.monotonic()-start
        assert 5.8 <= elapsed <= 8, elapsed
        assert len(relay.attempts) == 4, 'startup retry did not consume the shared budget'
        assert_retry_gaps(relay.attempts[1:], [2, 4])
    print('PASS: startup retry consumes the same budget; the following outage has only 2/4-second retries')


def reconnect_authentication_rejection(fixture):
    from trust_cases import observed_authentication, authentication_count
    with observed_authentication(fixture), SSHRelay(fixture) as relay:
        process = fixture.ready()
        before = authentication_count(fixture)
        try:
            fixture.docker('chpasswd', input=b'proxrs:changed-disposable-password\n')
            relay.change('reject')
            wait_for(lambda: not available(fixture), 2, 'outage before authentication recheck')
            relay.change('forward')
            assert process.wait(6) == 1
            assert len(relay.attempts) == 2 and authentication_count(fixture) == before+1
            _, _, err = fixture.processes[-1]
            err.seek(0)
            assert 'authentication rejected' in err.read()
        finally:
            fixture.docker('chpasswd', input=('proxrs:' + fixture.password + '\n').encode())
    print('PASS: real password rejection on reconnect stops after one new authentication attempt')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', default='ubuntu:24.04')
    options = parser.parse_args()
    with Fixture(options.image) as fixture:
        startup_transient(fixture)
        startup_budget_carries_forward(fixture)
        established_recovery(fixture)
        authentication_disconnect(fixture)
        bounded_startup(fixture)
        flapping_and_stability(fixture)
        silent_loss(fixture)
        startup_cancellation(fixture)
        no_replay(fixture)
        terminal_failures(fixture)
        reconnect_authentication_rejection(fixture)
        managed_startup(fixture)
        managed_recovery(fixture)
        managed_cancellation(fixture)


if __name__ == '__main__':
    main()
