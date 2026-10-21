#!/usr/bin/env python3
"""Opt-in managed-command acceptance through the real binary and a real PTY."""
import argparse
from proxy_cases import run_suite
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import contextlib
from urllib.parse import urlsplit

from proxy_cases import Fixture, ROOT
from trust_cases import TerminalProcess

PROXY_NAMES = ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'NO_PROXY', 'no_proxy', 'ALL_PROXY', 'all_proxy')
LOOPBACK = 'localhost,127.0.0.1,::1'


def run_args(fixture, child_args, inherit=False):
    args = fixture.args()
    args[1] = 'run'
    if inherit:
        args.append('--inherit-no-proxy')
    return args + ['--', sys.executable, str(ROOT / 'tests/docker/managed_child.py'), *child_args]


def run_child(fixture, mode='report', extra=(), inherit=False, env=None, password=None):
    return subprocess.run(run_args(fixture, [mode, str(fixture.path / 'ca.pem'), *extra], inherit),
                          input=((password or fixture.password) + '\nleftover-secret-input\n').encode(),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                          start_new_session=True, timeout=20)


def report(result, fixture):
    assert fixture.password.encode() not in result.stdout + result.stderr, 'password leaked'
    assert b'export HTTP_PROXY' not in result.stdout, result.stdout
    assert result.returncode == 23, (result.returncode, result.stderr.decode())
    data = json.loads(result.stdout)
    assert data['https'] == 'proxrs-docker-origin\n', data
    assert data['stdin'] == '', 'password stream reached the managed command'
    assert data['pid'] == data['pgid'], data
    assert b'controlled-child-stderr' in result.stderr
    port = urlsplit(data['env']['HTTP_PROXY']).port
    with socket.socket() as probe:
        assert probe.connect_ex(('127.0.0.1', port)) != 0, 'listener survived child completion'
    return data


def basic_command(fixture):
    arguments = ['two words', '$(touch should-not-exist)', '; echo unsafe', '--flag', 'line\nbreak']
    env = os.environ.copy()
    env.update({name: '*' if 'no_proxy' in name.lower() else 'inherited.invalid:7' for name in PROXY_NAMES})
    result = run_child(fixture, extra=arguments, env=env)
    data = report(result, fixture)
    assert data['args'] == arguments, data
    proxy = data['env']['HTTP_PROXY']
    assert proxy.startswith('http://127.0.0.1:') and not proxy.endswith(':0')
    for name in PROXY_NAMES[:4]:
        assert data['env'][name] == proxy, data
    assert data['env']['NO_PROXY'] == data['env']['no_proxy'] == LOOPBACK, data
    assert data['env']['ALL_PROXY'] is None and data['env']['all_proxy'] is None, data
    print('PASS: literal args, readiness/TLS, child-only environment, headless null input, status and listener cleanup')


def exclusion_matrix(fixture):
    cases = [({}, LOOPBACK), ({'NO_PROXY': 'upper.example'}, 'upper.example,' + LOOPBACK),
             ({'no_proxy': 'lower.example', 'NO_PROXY': 'upper.example'}, 'lower.example,' + LOOPBACK),
             ({'no_proxy': '', 'NO_PROXY': '*'}, LOOPBACK), ({'no_proxy': '*', 'NO_PROXY': 'upper.example'}, '*'),
             ({'NO_PROXY': '*'}, '*')]
    for overrides, expected in cases:
        env = {name: value for name, value in os.environ.items() if name not in PROXY_NAMES}
        env.update(overrides)
        data = report(run_child(fixture, inherit=True, env=env), fixture)
        assert data['env']['NO_PROXY'] == data['env']['no_proxy'] == expected, data
    rejected = subprocess.run([*fixture.args(), '--inherit-no-proxy'], input=b'', capture_output=True)
    assert rejected.returncode == 2 and b'--inherit-no-proxy' in rejected.stderr
    print('PASS: explicit exclusions, empty lowercase precedence, wildcard preservation, run-only option')


def terminal_input(fixture):
    for piped, queued in ((False, False), (True, False), (False, True)):
        args = run_args(fixture, ['interactive', str(fixture.path / 'ca.pem')])
        if not piped:
            args.remove('--password-stdin')
        child_pid = None
        args = [sys.executable, str(ROOT / 'tests/docker/managed_terminal.py'), *args]
        with TerminalProcess(fixture, args, password_pipe=piped) as process:
            try:
                if not piped:
                    process.wait_text('SSH password:')
                    process.send(fixture.password + '\n' + ('terminal-command-input\n' if queued else ''))
                output = process.wait_text('CHILD_READY ')
                child = json.loads(re.search(r'CHILD_READY (.+)', output)[1])
                child_pid = child['pid']
                assert child['tty'] and child['blocking'] and child['pid'] == child['pgid'] == child['foreground'], child
                if not queued:
                    process.send('\x03')
                    process.wait_text('CHILD_HANDLED_INT_AND_HTTPS')
                    process.send('terminal-command-input\n')
                process.wait_text('CHECK_RESTORED_INPUT')
                process.send('next-foreground-command\n')
                assert process.wait() == 7, process.read(0)
                assert 'TERMINAL_RESTORED_TO_EXISTING_PROCESS' in process.read(0)
                process.restored()
                process.out.seek(0)
                data = json.loads(process.out.read())
                assert data['entered'] == 'terminal-command-input\n', data
                assert data['https'] == 'proxrs-docker-origin\n', data
            finally:
                if child_pid:
                    try:
                        os.killpg(child_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
    print('PASS: foreground terminal input after hidden and piped password, settings restored')


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def descendant_cleanup(fixture):
    from recovery_cases import SSHRelay, wait_for
    sentinel = subprocess.Popen(['sleep', '90'], start_new_session=True)
    try:
        for stubborn in (False, True):
            for finish in ('exit', 'terminate', 'interrupt', 'ssh_loss'):
                relay_context = SSHRelay(fixture) if finish == 'ssh_loss' else contextlib.nullcontext()
                with relay_context as relay:
                    out = open(fixture.path / 'managed-out', 'w+')
                    err = open(fixture.path / 'managed-err', 'w+')
                    process = subprocess.Popen(run_args(fixture, ['descendants', str(fixture.path / 'ca.pem'), 'stubborn' if stubborn else 'cooperative', finish]),
                                               stdin=subprocess.PIPE, stdout=out, stderr=err, start_new_session=True)
                    child = None
                    try:
                        process.stdin.write((fixture.password + '\n').encode())
                        process.stdin.close()
                        deadline = time.monotonic() + 15
                        while time.monotonic() < deadline:
                            out.seek(0)
                            line = out.readline()
                            if line:
                                child = json.loads(line)
                                break
                            assert process.poll() is None, 'managed command failed before child readiness'
                            time.sleep(.03)
                        assert child is not None, 'managed command did not start'
                        time.sleep(.2)
                        start = time.monotonic()
                        if finish == 'terminate':
                            process.terminate()
                        elif finish == 'interrupt':
                            process.send_signal(signal.SIGINT)
                        elif finish == 'ssh_loss':
                            relay.change('reject')
                            time.sleep(.2)
                            assert alive(child['pid']) and alive(child['descendant']), 'retry killed the managed command early'
                            assert process.poll() is None
                        port = urlsplit(child['proxy']).port
                        def listener_closed():
                            with socket.socket() as probe:
                                return probe.connect_ex(('127.0.0.1', port)) != 0
                        if stubborn:
                            wait_for(listener_closed, 10 if relay else 1, 'listener closure before shutdown grace')
                            assert alive(child['descendant']), 'stubborn descendant did not receive a grace period'
                        status = process.wait(timeout=15 if relay else 8)
                        out.seek(0)
                        err.seek(0)
                        assert fixture.password not in out.read() + err.read(), 'password leaked'
                        elapsed = time.monotonic() - start
                        assert status == (17 if finish == 'exit' else 1 if finish == 'ssh_loss' else 0), status
                        assert not alive(child['pid']) and not alive(child['descendant']), 'owned process survived shutdown'
                        retry_seconds = 7 if relay else 0
                        if stubborn:
                            assert retry_seconds+4.5 <= elapsed <= retry_seconds+7, elapsed
                        else:
                            assert elapsed < retry_seconds+3, elapsed
                            if relay:
                                assert elapsed >= retry_seconds-.5, elapsed
                        if relay:
                            assert len(relay.attempts) == 4, relay.attempts
                        assert sentinel.poll() is None, 'unrelated process was signaled'
                        assert listener_closed()
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait()
                        if child:
                            with contextlib.suppress(ProcessLookupError):
                                os.killpg(child['pid'], signal.SIGKILL)
                        out.close()
                        err.close()
    finally:
        sentinel.terminate()
        sentinel.wait()
    print('PASS: cooperative/stubborn cleanup on completion, directed signals and exhausted retries; unrelated process survives')


def startup_and_launch_failures(fixture):
    failed = run_child(fixture, password='wrong-disposable-password')
    assert failed.returncode == 1 and not failed.stdout
    assert b'authentication rejected' in failed.stderr
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1', 0))
        occupied.listen()
        args = run_args(fixture, ['report', str(fixture.path / 'ca.pem')])
        args[args.index('--port') + 1] = str(occupied.getsockname()[1])
        failed = subprocess.run(args, input=b'', capture_output=True, start_new_session=True, timeout=3)
        assert failed.returncode == 1 and not failed.stdout and b'could not bind' in failed.stderr
    for kind in ('launch_failure', 'terminate', 'startup_interrupt'):
        args = run_args(fixture, ['interactive', str(fixture.path / 'ca.pem')])
        if kind == 'launch_failure':
            args = args[:args.index('--') + 1] + [str(fixture.path / 'command-does-not-exist')]
        if kind == 'startup_interrupt':
            args.remove('--password-stdin')
        wrapped = [sys.executable, str(ROOT / 'tests/docker/managed_terminal.py'), *args]
        child_pid = None
        with TerminalProcess(fixture, wrapped, password_pipe=kind != 'startup_interrupt') as process:
            try:
                output = process.wait_text('PROXRS_PID ')
                proxy_pid = int(re.search(r'PROXRS_PID (\d+)', output)[1])
                if kind == 'startup_interrupt':
                    process.wait_text('SSH password:')
                    process.send('\x03')
                elif kind == 'terminate':
                    output = process.wait_text('CHILD_READY ')
                    child_pid = json.loads(re.search(r'CHILD_READY (.+)', output)[1])['pid']
                    os.kill(proxy_pid, signal.SIGTERM)
                else:
                    process.wait_text('could not launch managed command')
                process.wait_text('CHECK_RESTORED_INPUT')
                process.send('next-foreground-command\n')
                assert process.wait() == (1 if kind == 'launch_failure' else 0), process.read(0)
                assert 'TERMINAL_RESTORED_TO_EXISTING_PROCESS' in process.read(0)
                process.restored()
            finally:
                if child_pid:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(child_pid, signal.SIGKILL)
    print('PASS: readiness gates child launch; launch failure, startup Ctrl-C and directed termination restore existing foreground process')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', default='ubuntu:24.04')
    options = parser.parse_args()
    with Fixture(options.image) as fixture:
        basic_command(fixture)
        exclusion_matrix(fixture)
        terminal_input(fixture)
        descendant_cleanup(fixture)
        startup_and_launch_failures(fixture)


if __name__ == '__main__':
    run_suite(main)
