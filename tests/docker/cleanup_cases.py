#!/usr/bin/env python3
"""Interrupt an actual fixture owner and observe that all owned resources exit."""
import argparse
from proxy_cases import run_suite
import contextlib
import json
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

from proxy_cases import Fixture


def hold_fixture(image, startup=False):
    fixture = Fixture(image)
    if startup:
        print(json.dumps(dict(directory=str(fixture.path))), flush=True)
    with fixture:
        process = fixture.ready()
        fixture.https()
        print(json.dumps(dict(container=fixture.container, directory=str(fixture.path),
                              proxy_pid=process.pid, proxy_port=fixture.proxy_port)), flush=True)
        signal.pause()


def exists(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def interruption(image, phase):
    wrapper_directory = tempfile.TemporaryDirectory(prefix='proxrs-cleanup-check-')
    wrapper_path = Path(wrapper_directory.name)
    report = wrapper_path / 'container.json'
    environment = dict(os.environ)
    if phase == 'startup':
        # The real daemon creates its container, but this Docker CLI boundary
        # pauses before delivering the ID to the fixture owner. No SSH mocking.
        docker = shutil.which('docker')
        wrapper = wrapper_path / 'docker'
        wrapper.write_text(f'''#!{sys.executable}
import json, os, pathlib, subprocess, sys, time
args = sys.argv[1:]
if args and args[0] == 'run':
    result = subprocess.run([{docker!r}] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode == 0:
        report = pathlib.Path({str(report)!r})
        temporary = report.with_suffix('.tmp')
        temporary.write_text(json.dumps(dict(container=result.stdout.decode().strip())))
        temporary.replace(report)
        deadline = time.monotonic() + 60
        while not report.with_suffix('.release').exists() and time.monotonic() < deadline:
            time.sleep(.01)
    sys.stdout.buffer.write(result.stdout)
    sys.stderr.buffer.write(result.stderr)
    sys.exit(result.returncode)
os.execv({docker!r}, [{docker!r}] + args)
''')
        wrapper.chmod(0o700)
        environment['PATH'] = str(wrapper_path) + os.pathsep + environment.get('PATH', '')
    owner = subprocess.Popen([sys.executable, '-u', __file__, '--image', image, '--hold-fixture', '--phase', phase],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             start_new_session=True, env=environment)
    sentinel = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
                                start_new_session=True)
    resources = None
    try:
        assert select.select([owner.stdout], [], [], 90)[0], 'fixture owner did not become ready'
        line = owner.stdout.readline()
        if not line:
            raise AssertionError('fixture owner exited: ' + owner.stderr.read())
        resources = json.loads(line)
        if phase == 'startup':
            deadline = time.monotonic() + 90
            while not report.exists():
                assert owner.poll() is None, 'fixture owner exited during startup'
                assert time.monotonic() < deadline, 'container did not start'
                time.sleep(.05)
            resources.update(json.loads(report.read_text()))
        owner.terminate()
        if phase == 'startup':
            time.sleep(.1)
            report.with_suffix('.release').touch()
        status = owner.wait(timeout=15)
        assert status in (-signal.SIGTERM, 128 + signal.SIGTERM), status
        inspected = subprocess.run(['docker', 'inspect', resources['container']],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert inspected.returncode != 0, 'fixture container survived harness SIGTERM'
        if 'proxy_pid' in resources:
            assert not exists(resources['proxy_pid']), 'proxrs process survived harness SIGTERM'
        assert not Path(resources['directory']).exists(), 'credential directory survived harness SIGTERM'
        if 'proxy_port' in resources:
            with contextlib.suppress(ConnectionRefusedError):
                with socket.create_connection(('127.0.0.1', resources['proxy_port']), timeout=1):
                    raise AssertionError('proxy listener survived harness SIGTERM')
        assert sentinel.poll() is None, 'unrelated process was terminated'
        owned = 'proxy, listener, container, and credentials' if phase == 'ready' else 'starting container and credentials'
        print(f'PASS {image} {phase}: harness SIGTERM removes its {owned}; unrelated process survives')
    finally:
        if owner.poll() is None:
            owner.terminate()
            try:
                owner.wait(timeout=15)
            except subprocess.TimeoutExpired:
                owner.kill()
                owner.wait(timeout=5)
        # Failure cleanup is scoped to the resources the child reported owning.
        if resources:
            if 'proxy_pid' in resources and exists(resources['proxy_pid']):
                os.kill(resources['proxy_pid'], signal.SIGTERM)
                deadline = time.monotonic() + 8
                while exists(resources['proxy_pid']) and time.monotonic() < deadline:
                    time.sleep(.05)
                if exists(resources['proxy_pid']):
                    os.kill(resources['proxy_pid'], signal.SIGKILL)
            if 'container' in resources:
                subprocess.run(['docker', 'rm', '--force', resources['container']],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            shutil.rmtree(resources['directory'], ignore_errors=True)
        sentinel.terminate()
        sentinel.wait(timeout=5)
        owner.stdout.close()
        owner.stderr.close()
        wrapper_directory.cleanup()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', default='ubuntu:24.04')
    parser.add_argument('--phase', choices=['ready', 'startup'])
    parser.add_argument('--hold-fixture', action='store_true', help=argparse.SUPPRESS)
    options = parser.parse_args()
    if options.hold_fixture:
        hold_fixture(options.image, startup=options.phase == 'startup')
    else:
        for phase in [options.phase] if options.phase else ['ready', 'startup']:
            interruption(options.image, phase)


if __name__ == '__main__':
    run_suite(main)
