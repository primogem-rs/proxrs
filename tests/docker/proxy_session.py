"""Own one acceptance invocation independently of the reusable SSH fixture."""
import contextlib
import fcntl
import os
from pathlib import Path
import pty
import re
import select
import signal
import socket
import subprocess
import tempfile
import termios
import time
from typing import Optional


def run_suite(main):
    """Unwind active owners on termination; repeated signals cannot cut cleanup short."""
    def terminate(signum, _frame):
        for number in (signal.SIGINT, signal.SIGTERM):
            signal.signal(number, signal.SIG_IGN)
        raise SystemExit(128 + signum)

    previous = {number: signal.signal(number, terminate)
                for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        main()
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


@contextlib.contextmanager
def defer_termination():
    """Finish acquisition or cleanup before delivering ordinary termination."""
    pending = []
    def defer(signum, _frame):
        if not pending:
            pending.append(signum)

    previous = {number: signal.signal(number, defer)
                for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
        if pending:
            number = pending[0]
            handler = previous[number]
            if callable(handler):
                handler(number, None)
            elif handler != signal.SIG_IGN:
                raise SystemExit(128 + number)


class CleanupError(RuntimeError):
    pass


def cleanup_all(actions):
    """Attempt every resource, preserving any original failure in the exception chain."""
    failures = []
    for label, action in actions:
        try:
            action()
        except Exception as error:
            failures.append(f'{label}: {error}')
    if failures:
        raise CleanupError('; '.join(failures))


def register_managed_group():
    """Controlled helpers publish ownership before reads, networking, or forking."""
    directory = os.environ.get('PROXRS_TEST_OWNERSHIP')
    if directory:
        path = Path(directory) / str(os.getpid())
        temporary = path.with_suffix('.tmp')
        temporary.write_text(str(os.getpgrp()))
        temporary.replace(path)


class PipeIO:
    def __init__(self):
        with contextlib.ExitStack() as resources:
            self.out = resources.enter_context(tempfile.TemporaryFile())
            self.err = resources.enter_context(tempfile.TemporaryFile())
            self.resources = resources.pop_all()

    def launch(self, args, env, password_input):
        process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=self.out,
                                   stderr=self.err, start_new_session=True, env=env)
        return process

    def output(self, stream):
        handle = self.out if stream == 'stdout' else self.err
        return os.pread(handle.fileno(), os.fstat(handle.fileno()).st_size, 0).decode(errors='replace')

    def close(self):
        self.resources.close()


class TerminalIO:
    def __init__(self):
        self.output_bytes = bytearray()
        with contextlib.ExitStack() as resources:
            self.out = resources.enter_context(tempfile.TemporaryFile())
            self.master, self.slave = pty.openpty()
            resources.callback(os.close, self.master)
            resources.callback(os.close, self.slave)
            self.before = termios.tcgetattr(self.slave)
            resources.pop_all()  # Descriptors below close independently on partial launch.
        self.read_pipe = self.write_pipe = None

    def launch(self, args, env, password_input):
        if password_input is not None:
            self.read_pipe, self.write_pipe = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                slave = self.slave
                assert slave is not None
                os.setsid()
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
                os.dup2(slave if self.read_pipe is None else self.read_pipe, 0)
                os.dup2(self.out.fileno(), 1)
                os.dup2(slave, 2)
                os.close(self.master)
                if self.read_pipe is not None:
                    assert self.write_pipe is not None
                    os.close(self.write_pipe)
                    os.close(self.read_pipe)
                if slave > 2:
                    os.close(slave)
                os.execvpe(args[0], args, env)
            except BaseException as error:
                os.write(2, f'terminal exec failed: {error}\n'.encode())
                os._exit(127)
        return pid

    def finish_launch(self, password_input):
        assert self.slave is not None
        os.close(self.slave)
        self.slave = None
        if self.read_pipe is not None:
            assert self.write_pipe is not None
            os.close(self.read_pipe)
            self.read_pipe = None
            try:
                os.write(self.write_pipe, password_input)
            except BrokenPipeError:
                pass
            finally:
                os.close(self.write_pipe)
                self.write_pipe = None

    def read(self, timeout=.05):
        if select.select([self.master], [], [], timeout)[0]:
            try:
                self.output_bytes.extend(os.read(self.master, 65536))
            except OSError:
                pass  # A closed PTY reports EIO on some platforms.
        return self.output_bytes.decode(errors='replace')

    def output(self, stream):
        if stream == 'stderr':
            return self.read(0)
        return os.pread(self.out.fileno(), os.fstat(self.out.fileno()).st_size, 0).decode(errors='replace')

    def close(self):
        actions = [('stdout', self.out.close), ('PTY master', lambda: os.close(self.master))]
        for name in ('slave', 'read_pipe', 'write_pipe'):
            descriptor = getattr(self, name)
            if descriptor is not None:
                actions.append((name, lambda fd=descriptor: os.close(fd)))
        cleanup_all(actions)


class InheritedIO:
    """The waiting foreground helper leaves proxrs on its existing descriptors."""
    def launch(self, args, env, password_input):
        return subprocess.Popen(args, env=env)

    def output(self, stream):
        return ''  # The outer PTY owner captures these inherited descriptors.

    def close(self):
        pass


class ProxySession:
    """Launch on context entry. Readiness and product assertions belong to the case."""
    def __init__(self, args, *, password_input: Optional[bytes] = b'', env=None, secrets=(), terminal=False, inherited=False):
        self.args = args
        self.password_input = password_input
        self.env = env
        self.secrets = secrets
        self.is_terminal = terminal
        self.is_inherited = inherited
        self.pid = None
        self.returncode = None
        self.usage = None
        self.port = None
        self.io = None
        self._process = None
        self.directory = None
        self.ownership_path = None
        self._closed = False
        self._stdout = self._stderr = None

    @classmethod
    def pipe(cls, args, **kwargs):
        return cls(args, **kwargs)

    @classmethod
    def terminal(cls, args, *, password_input=None, **kwargs):
        return cls(args, password_input=password_input, terminal=True, **kwargs)

    @classmethod
    def inherited(cls, args):
        return cls(args, inherited=True)

    def __enter__(self):
        try:
            with defer_termination():
                env = dict(os.environ if self.env is None else self.env)
                if not self.is_inherited or 'PROXRS_TEST_OWNERSHIP' not in env:
                    self.directory = tempfile.TemporaryDirectory(prefix='proxrs-session-')
                    env['PROXRS_TEST_OWNERSHIP'] = self.directory.name
                self.ownership_path = Path(env['PROXRS_TEST_OWNERSHIP'])
                self.io = TerminalIO() if self.is_terminal else InheritedIO() if self.is_inherited else PipeIO()
                launched = self.io.launch(self.args, env, self.password_input)
                if isinstance(launched, int):
                    self.pid = launched
                else:
                    self._process = launched
                    self.pid = launched.pid
            if isinstance(self.io, TerminalIO):
                self.io.finish_launch(self.password_input)
            elif not self.is_inherited:
                assert self._process is not None and self._process.stdin is not None
                try:
                    if self.password_input:
                        self._process.stdin.write(self.password_input)
                    self._process.stdin.close()
                except BrokenPipeError:
                    pass
            return self
        except BaseException:
            self.close()
            raise

    @property
    def stdout(self):
        if self._stdout is not None:
            return self._stdout
        assert self.io is not None
        return self.io.output('stdout')

    @property
    def stderr(self):
        if self._stderr is not None:
            return self._stderr
        assert self.io is not None
        return self.io.output('stderr')

    def poll(self):
        assert self.pid is not None, 'session has not launched'
        if self.returncode is None:
            pid, status, usage = os.wait4(self.pid, os.WNOHANG)
            if pid:
                self.returncode = os.waitstatus_to_exitcode(status)
                self.usage = usage
                if self._process is not None:
                    self._process.returncode = self.returncode
        return self.returncode

    def wait(self, timeout=5):
        deadline = time.monotonic() + timeout
        while self.poll() is None:
            if isinstance(self.io, TerminalIO):
                self.io.read(0)  # Output policy is checked after cleanup, not while reaping.
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.args, timeout)
            time.sleep(.02)
        return self.returncode

    def send_signal(self, number):
        assert self.pid is not None, 'session has not launched'
        if self.poll() is None:
            os.kill(self.pid, number)

    def terminate(self):
        self.send_signal(signal.SIGTERM)

    @property
    def master(self):
        assert isinstance(self.io, TerminalIO), 'session has no PTY'
        return self.io.master

    @property
    def before(self):
        assert isinstance(self.io, TerminalIO), 'session has no PTY'
        return self.io.before

    def read(self, timeout=.05):
        assert isinstance(self.io, TerminalIO), 'session has no PTY'
        output = self.io.read(timeout)
        for secret in self.secrets:
            assert secret not in output, 'sensitive data appeared on terminal'
        return output

    def wait_text(self, text, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            output = self.read()
            if text in output:
                return output
            if self.poll() is not None:
                raise AssertionError('process exited before prompt: ' + output)
        raise AssertionError('missing prompt: ' + text + ': ' + self.read(0))

    def send(self, text):
        os.write(self.master, text.encode())

    def restored(self):
        assert termios.tcgetattr(self.master) == self.before, 'terminal settings were not restored'

    def ready(self, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.is_terminal:
                self.read(0)
            exports = dict(re.findall(r"^export (\w+)='([^']*)'\r?$", self.stdout, re.M))
            if all(name in exports for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'NO_PROXY', 'no_proxy')):
                url = exports['HTTP_PROXY']
                assert all(exports[name] == url for name in ('HTTPS_PROXY', 'http_proxy', 'https_proxy')), exports
                assert exports['NO_PROXY'] == exports['no_proxy'] == 'localhost,127.0.0.1,::1', exports
                self.observe_proxy_url(url)
                return self
            if self.poll() is not None:
                raise AssertionError('proxy exited before readiness: ' + self.stdout + self.stderr)
            time.sleep(.03)
        raise AssertionError('proxy did not become ready: ' + self.stdout + self.stderr)

    def observe_proxy_url(self, url):
        match = re.fullmatch(r'http://127\.0\.0\.1:([0-9]+)', url)
        assert match and 0 < int(match[1]) <= 65535, url
        port = int(match[1])
        assert self.port in (None, port), 'proxy address changed within one invocation'
        self.port = port

    def connect(self, authority='origin.proxrs.test:8443', status=200):
        stream = socket.create_connection(('127.0.0.1', self.port), timeout=15)
        try:
            stream.sendall(f'CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\nProxy-Authorization: Basic proxrs-sensitive-header-marker\r\n\r\n'.encode())
            response = b''
            while b'\r\n\r\n' not in response:
                data = stream.recv(1)
                if not data:
                    break
                response += data
            assert response.startswith(f'HTTP/1.1 {status} '.encode()), response
            return stream
        except BaseException:
            stream.close()
            raise

    def _stop(self):
        if self.pid is None:
            return
        if self.poll() is None:
            try:
                self.terminate()
                try:
                    self.wait(8)
                except subprocess.TimeoutExpired:
                    pass
            finally:
                if self.poll() is None:
                    os.kill(self.pid, signal.SIGKILL)
                    deadline = time.monotonic() + 5
                    # Even a broken PTY must not prevent the final wait4.
                    while self.poll() is None:
                        if time.monotonic() >= deadline:
                            raise subprocess.TimeoutExpired(self.args, 5)
                        time.sleep(.02)

    def _stop_groups(self):
        if self.is_inherited:
            # The outer PTY owner lets its case observe product group cleanup
            # after this waiting helper exits, before emergency cleanup runs.
            return
        groups = {self.pid} if self.pid is not None else set()
        if self.ownership_path is not None:
            for path in self.ownership_path.iterdir():
                if path.name.isdigit():
                    group = int(path.read_text())
                    assert group > 1 and group != os.getpgrp(), 'invalid managed process group'
                    groups.add(group)
        def stop(group):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(group, signal.SIGKILL)
                deadline = time.monotonic() + 5
                while True:
                    os.killpg(group, 0)
                    assert time.monotonic() < deadline, f'process group {group} survived cleanup'
                    time.sleep(.02)
        cleanup_all([(f'process group {group}', lambda group=group: stop(group)) for group in groups])

    def _check_output(self):
        self._stdout, self._stderr = self.stdout, self.stderr
        output = self.stdout + self.stderr
        for secret in self.secrets:
            assert secret not in output, 'sensitive data appeared in session output'

    @defer_termination()
    def close(self):
        if self._closed:
            return
        self._closed = True
        actions = [('process', self._stop), ('process groups', self._stop_groups)]
        if self.io is not None:
            actions.extend([('output policy', self._check_output), ('output handles', self.io.close)])
        if self._process is not None and self._process.stdin is not None:
            actions.append(('password input', self._process.stdin.close))
        if self.directory is not None:
            actions.append(('ownership directory', self.directory.cleanup))
        cleanup_all(actions)

    def __exit__(self, *args):
        self.close()
