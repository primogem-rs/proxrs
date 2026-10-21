"""Test acceptance-session ownership with real local children, without Docker."""
import os
import contextlib
from pathlib import Path
import sys
import termios
import tempfile
import time
import subprocess
import signal
import shlex
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'docker'))
from proxy_session import CleanupError, ProxySession


def child(source):
    return [sys.executable, '-u', '-c', source]


class SessionOwnership(unittest.TestCase):
    def test_waiting_shell_helper_keeps_following_terminal_input_for_the_shell(self):
        helper = Path(__file__).resolve().parents[1] / 'docker' / 'wait_proxy.py'
        command = child("import sys; tty = open('/dev/tty', 'rb+', buffering=0); print('CHILD_INPUT', file=sys.stderr, flush=True); print(tty.readline().decode(), end='', flush=True)")
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / 'proxy.pid'
            script = (shlex.join([sys.executable, str(helper), str(pid_file), *command])
                      + ' & child=$!; wait "$child"; IFS= read -r next; test "$next" = next-command')
            with ProxySession.terminal(['/bin/sh', '-c', script]) as session:
                session.wait_text('CHILD_INPUT')
                session.send('child-command\nnext-command\n')
                self.assertEqual(session.wait(3), 0, session.stderr)
                self.assertEqual(session.stdout, 'child-command\n')
                self.assertGreater(int(pid_file.read_text()), 1)

    def test_output_violation_during_pty_shutdown_still_reaps_and_caches_usage(self):
        source = '''
import signal, sys, time
def stop(*args):
    time.sleep(.2)
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
print('private-marker STARTED', file=sys.stderr, flush=True)
while True: time.sleep(1)
'''
        session = ProxySession.terminal(child(source), secrets=('private-marker',))
        try:
            with self.assertRaises(CleanupError):
                with session:
                    deadline = time.monotonic() + 3
                    while 'STARTED' not in session.stderr:
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(.01)
            self.assertIsNotNone(session.returncode)
            self.assertIsNotNone(session.usage)
            with self.assertRaises(ChildProcessError):
                os.waitpid(session.pid, os.WNOHANG)
        finally:
            with contextlib.suppress(ChildProcessError):
                os.waitpid(session.pid, 0)

    def test_nested_helper_does_not_hide_a_product_group_cleanup_failure(self):
        command_source = '''
import os, time
from pathlib import Path
from proxy_session import register_managed_group
pid = os.fork()
if pid == 0:
    os.setpgrp()
    register_managed_group()
    print(os.getpid(), flush=True)
    while True: time.sleep(1)
deadline = time.monotonic() + 3
while not (Path(os.environ['PROXRS_TEST_OWNERSHIP']) / str(pid)).exists():
    assert time.monotonic() < deadline
    time.sleep(.01)
'''
        helper_source = f'''
from proxy_session import ProxySession
with ProxySession.inherited({child(command_source)!r}) as session:
    assert session.wait(3) == 0
'''
        env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'docker')}
        with ProxySession.pipe(child(helper_source), env=env) as session:
            self.assertEqual(session.wait(3), 0, session.stderr)
            managed = int(session.stdout)
            os.kill(managed, 0)  # The acceptance case can still detect the survivor.
        with self.assertRaises(ProcessLookupError):
            os.kill(managed, 0)

    def test_cleanup_failure_keeps_original_error_and_removes_remaining_resources(self):
        session = ProxySession.pipe(child("import os; print(os.environ['PROXRS_TEST_OWNERSHIP'], flush=True); print('private-marker', flush=True)"),
                                    secrets=('private-marker',))
        with self.assertRaises(CleanupError) as caught:
            with session:
                self.assertEqual(session.wait(3), 0)
                directory = Path(session.stdout.splitlines()[0])
                raise ValueError('original case failure')
        self.assertIsInstance(caught.exception.__context__, ValueError)
        self.assertIn('original case failure', str(caught.exception.__context__))
        self.assertIn('sensitive data', str(caught.exception))
        self.assertFalse(directory.exists())
        session.close()  # Repeated cleanup is safe, including after a failure.

    def test_partial_export_block_is_not_readiness(self):
        with ProxySession.pipe(child("import time; print(\"export HTTP_PROXY='http://127.0.0.1:12345'\", flush=True); time.sleep(60)")) as session:
            with self.assertRaisesRegex(AssertionError, 'did not become ready'):
                session.ready(timeout=.1)
            self.assertIsNone(session.port)
            self.assertIsNone(session.poll())

    def test_cancellation_during_pipe_and_pty_acquisition_registers_the_child(self):
        for adapter in ('pipe', 'terminal'):
            for number in (signal.SIGINT, signal.SIGTERM):
                with self.subTest(adapter=adapter, signal=number), tempfile.TemporaryDirectory() as directory:
                    report = Path(directory) / 'child'
                    source = f'''
import os, subprocess
from pathlib import Path
from proxy_session import ProxySession, run_suite
original = {'subprocess.Popen' if adapter == 'pipe' else 'os.fork'}
def acquire(*args, **kwargs):
    acquired = original(*args, **kwargs)
    pid = {'acquired.pid' if adapter == 'pipe' else 'acquired'}
    if pid:
        Path({str(report)!r}).write_text(str(pid))
        os.kill(os.getpid(), {int(number)})
    return acquired
{'subprocess.Popen' if adapter == 'pipe' else 'os.fork'} = acquire
def main():
    with ProxySession.{adapter}({child('import time; time.sleep(60)')!r}):
        raise AssertionError('termination was not delivered after acquisition')
run_suite(main)
'''
                    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'docker')}
                    result = subprocess.run(child(source), env=env, capture_output=True, timeout=15)
                    self.assertEqual(result.returncode, 128 + number, result.stderr.decode())
                    pid = int(report.read_text())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)

    def test_cancellation_during_acquisition_cleans_an_early_managed_group(self):
        managed_source = '''
import os, time
from proxy_session import register_managed_group
if os.fork() == 0:
    os.setpgrp()
    register_managed_group()
    while True: time.sleep(1)
while True: time.sleep(1)
'''
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'group'
            source = f'''
import os, subprocess, time
from pathlib import Path
from proxy_session import ProxySession, run_suite
original = subprocess.Popen
def acquire(*args, **kwargs):
    process = original(*args, **kwargs)
    end = time.monotonic() + 3
    owned = Path(kwargs['env']['PROXRS_TEST_OWNERSHIP'])
    while True:
        groups = [path for path in owned.iterdir() if path.name.isdigit()]
        if groups: break
        assert time.monotonic() < end
        time.sleep(.01)
    Path({str(report)!r}).write_text(groups[0].read_text())
    os.kill(os.getpid(), {int(signal.SIGTERM)})
    return process
subprocess.Popen = acquire
def main():
    with ProxySession.pipe({child(managed_source)!r}):
        raise AssertionError('entered despite cancellation')
run_suite(main)
'''
            env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'docker')}
            result = subprocess.run(child(source), env=env, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 143, result.stderr.decode())
            with self.assertRaises(ProcessLookupError):
                os.killpg(int(report.read_text()), 0)

    def test_nested_owner_inherits_the_existing_terminal_and_output(self):
        command = child("import sys; print('input: ' + sys.stdin.readline(), end='', flush=True)")
        source = f'''
import os
from proxy_session import ProxySession
with ProxySession.inherited({command!r}) as session:
    print('NESTED', file=__import__('sys').stderr, flush=True)
    assert os.getpgid(session.pid) == os.getpgrp()
    assert session.wait(3) == 0
'''
        env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'docker')}
        with ProxySession.terminal(child(source), env=env) as session:
            session.wait_text('NESTED')
            session.send('nested input\n')
            self.assertEqual(session.wait(3), 0, session.stderr)
            self.assertEqual(session.stdout, 'input: nested input\n')

    def test_failure_before_readiness_removes_managed_group_and_keeps_original_error(self):
        source = '''
import os, signal, time
from proxy_session import register_managed_group
pid = os.fork()
if pid == 0:
    os.setpgrp()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    register_managed_group()
    print(os.getpid(), flush=True)
    while True: time.sleep(1)
while True: time.sleep(1)
'''
        managed = None
        with self.assertRaisesRegex(RuntimeError, 'before readiness'):
            with ProxySession.pipe(child(source), env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'docker')}) as session:
                deadline = time.monotonic() + 3
                while not session.stdout:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.01)
                managed = int(session.stdout)
                raise RuntimeError('before readiness')
        with self.assertRaises(ProcessLookupError):
            assert managed is not None
            os.kill(managed, 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(session.pid, 0)

    def test_terminal_preserves_controlling_tty_and_queued_input(self):
        source = '''
import os, sys
with open('/dev/tty', 'rb+', buffering=0) as tty:
    print('PROMPT', file=sys.stderr, flush=True)
    entered = tty.readline()
    print(f'{os.getpid()} {os.tcgetpgrp(tty.fileno())}', flush=True)
    print(entered.decode(), end='', flush=True)
'''
        with ProxySession.terminal(child(source), password_input=b'password\n') as session:
            session.wait_text('PROMPT')
            self.assertEqual(os.tcgetpgrp(session.master), session.pid)
            session.send('queued input\n')
            self.assertEqual(session.wait(3), 0)
            self.assertEqual(session.stdout, f'{session.pid} {session.pid}\nqueued input\n')
            session.restored()
            self.assertEqual(termios.tcgetattr(session.master), session.before)

    def test_sessions_keep_independent_output_addresses_and_exit_usage(self):
        source = '''
import socket, sys, time
s = socket.socket()
s.bind(('127.0.0.1', 0))
s.listen()
port = s.getsockname()[1]
for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
    print(f"export {name}='http://127.0.0.1:{port}'", flush=True)
for name in ('NO_PROXY', 'no_proxy'):
    print(f"export {name}='localhost,127.0.0.1,::1'", flush=True)
print(sys.argv[1], file=sys.stderr, flush=True)
time.sleep(60)
'''
        with ProxySession.pipe(child(source) + ['first']) as first:
            first.ready()
            with ProxySession.pipe(child(source) + ['second']) as second:
                second.ready()
                self.assertNotEqual(first.port, second.port)
                self.assertIn('first', first.stderr)
                self.assertNotIn('second', first.stderr)
                self.assertIn('second', second.stderr)
                first.terminate()
                status = first.wait(3)
                usage = first.usage
                self.assertEqual(status, -15)
                self.assertEqual(first.poll(), status)
                self.assertEqual(first.wait(0), status)
                self.assertIs(first.usage, usage)
                self.assertGreater(usage.ru_maxrss, 0)
                with self.assertRaises(ChildProcessError):
                    os.waitpid(first.pid, os.WNOHANG)
                self.assertIsNone(second.poll())
        self.assertIn('first', first.stderr)
        self.assertIn('second', second.stderr)


if __name__ == '__main__':
    unittest.main()
