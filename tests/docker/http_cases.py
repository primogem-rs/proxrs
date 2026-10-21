#!/usr/bin/env python3
"""Public HTTP proxy protocol and streaming cases against real Docker SSH."""
import argparse
from proxy_cases import run_suite
from contextlib import closing, ExitStack
import http.client
from concurrent.futures import ThreadPoolExecutor
import socket
import json
import time
import ssl
import base64
import hashlib
import os
from proxy_cases import command
from proxy_cases import Fixture


def connection(process):
    return http.client.HTTPConnection('127.0.0.1', process.port, timeout=15)


def forwarding(fixture, process):
    with closing(connection(process)) as client:
        client.request('GET', 'http://origin.proxrs.test:8080/path?query=yes', headers={'Host': 'ignored.invalid'})
        response = client.getresponse()
        assert response.status == 200, response.status
        assert response.read() == b'proxrs-docker-origin\n'
    print('PASS: ordinary HTTP reaches the unpublished origin through remote DNS')


def status(process, data):
    with socket.create_connection(('127.0.0.1', process.port), timeout=15) as stream:
        stream.sendall(data)
        response = stream.recv(4096)
        return int(response.split(b' ', 2)[1]) if response else None


def authority_validation(fixture, process):
    malformed = [
        b'GET http://origin.proxrs.test:65536/ HTTP/1.1\r\nHost: x\r\n\r\n',
        b'GET http://origin.proxrs.test:abc/ HTTP/1.1\r\nHost: x\r\n\r\n',
        b'GET http://origin.proxrs.test:/ HTTP/1.1\r\nHost: x\r\n\r\n',
        b'GET http://[::1]garbage:8080/ HTTP/1.1\r\nHost: x\r\n\r\n',
        b'CONNECT [::1]garbage:8443 HTTP/1.1\r\nHost: x\r\n\r\n',
        b'CONNECT [::1]: HTTP/1.1\r\nHost: x\r\n\r\n',
        b'CONNECT [::1] HTTP/1.1\r\nHost: x\r\n\r\n',
    ]
    observed = [status(process, request) for request in malformed]
    assert observed == [400] * len(malformed), observed
    with closing(connection(process)) as client:
        client.request('GET', 'http://origin.proxrs.test/')
        response = client.getresponse()
        assert response.status == 200, response.status
        assert response.read() == b'proxrs-docker-origin\n'
    with closing(connection(process)) as client:
        client.request('GET', 'http://[::1]:8080/')
        response = client.getresponse()
        assert response.status == 200, response.status
        assert response.read() == b'proxrs-docker-origin\n'
    fixture.https(process, '[::1]:8443')
    print('PASS: malformed ports and complete IPv6 authorities return 400; absent HTTP port uses 80 and valid IPv6 forwards')


def framing(fixture, process):
    ambiguous = b'POST http://origin.proxrs.test:8080/ HTTP/1.1\r\nHost: origin.proxrs.test\r\nContent-Length: 0\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n'
    assert status(process, ambiguous) == 400
    print('PASS: ambiguous transfer framing is rejected')


def routing_and_headers(fixture, process):
    with closing(connection(process)) as client:
        original_socket = None
        for port in [8080, 8081, 8080]:
            client.request('GET', f'http://origin.proxrs.test:{port}/inspect?route={port}', headers={
                'Host': 'ignored.invalid', 'Connection': 'X-Remove, Keep-Alive',
                'X-Remove': 'must-not-forward', 'Proxy-Authorization': 'must-not-forward',
                'Proxy-Connection': 'keep-alive', 'Authorization': 'end-to-end-value'})
            response = client.getresponse()
            assert response.status == 200
            assert response.getheader('X-Origin-Hop') is None
            assert response.getheader('Proxy-Authenticate') is None
            observed = json.loads(response.read())
            if original_socket is None:
                original_socket = client.sock
            assert client.sock is original_socket and client.sock is not None, 'client connection was not reused'
            assert observed['port'] == port and observed['path'] == f'/inspect?route={port}', observed
            assert observed['host'] == f'origin.proxrs.test:{port}', observed
            headers = {key.lower(): value for key, value in observed['headers'].items()}
            assert headers['authorization'] == 'end-to-end-value'
            assert all(name not in headers for name in ['x-remove', 'proxy-authorization', 'proxy-connection', 'keep-alive'])
    environment = dict(os.environ)
    for name in ['HTTP_PROXY', 'HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy', 'NO_PROXY']:
        environment.pop(name, None)
    environment.update(http_proxy=f'http://127.0.0.1:{process.port}', no_proxy='localhost,127.0.0.1,::1')
    assert command('curl', '--fail', '--silent', '--show-error', 'http://origin.proxrs.test:8080/', env=environment) == b'proxrs-docker-origin\n'
    print('PASS: origin-form/Host, reused destinations, hop fields and lowercase curl environment')


def streaming(fixture, process):
    with closing(connection(process)) as client:
        started = time.monotonic()
        client.request('GET', 'http://origin.proxrs.test:8080/events')
        response = client.getresponse()
        assert response.readline() == b'data: first\n'
        assert time.monotonic() - started < 1.1, 'first event was buffered'
        assert response.read() == b'\ndata: second\n\n'
        assert time.monotonic() - started >= 2
    for chunked in [False, True]:
        with socket.create_connection(('127.0.0.1', process.port), timeout=15) as upload:
            framing = b'Transfer-Encoding: chunked' if chunked else b'Content-Length: 8192'
            upload.sendall(b'POST http://origin.proxrs.test:8080/upload HTTP/1.1\r\nHost: origin.proxrs.test\r\n' + framing + b'\r\n\r\n')
            upload.sendall((b'1000\r\n' if chunked else b'') + b'a' * 4096 + (b'\r\n' if chunked else b''))
            deadline = time.monotonic() + 3
            while True:
                with closing(connection(process)) as probe:
                    probe.request('GET', 'http://origin.proxrs.test:8080/uploaded')
                    count = int(probe.getresponse().read())
                if count == 4096:
                    break
                assert time.monotonic() < deadline, 'upload was buffered before reaching origin'
                time.sleep(.05)
            upload.sendall((b'1000\r\n' if chunked else b'') + b'b' * 4096 + (b'\r\n0\r\n\r\n' if chunked else b''))
            response = http.client.HTTPResponse(upload)
            response.begin()
            assert response.status == 200 and response.read() == b'8192'
    print('PASS: early event stream and incremental fixed/chunked uploads')


def deadlines_and_errors(fixture, process):
    malformed = [
        b'GET http://origin.proxrs.test:8080/ HTTP/1.1\nHost: x\n\n',
        b'GET / HTTP/1.1\r\nHost: x\r\n\r\n',
        b'GET http://origin.proxrs.test:8080/ HTTP/1.1\r\n\r\n',
        b'GET http://origin.proxrs.test:8080/ HTTP/1.1\r\nHost: x\r\nHost: y\r\n\r\n',
        b'POST http://origin.proxrs.test:8080/ HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\nContent-Length: 3\r\n\r\n',
        b'GET http://[not-ipv6]:80/ HTTP/1.1\r\nHost: x\r\n\r\n',
        b'GET http://origin.proxrs.test:0/ HTTP/1.1\r\nHost: x\r\n\r\n',
    ]
    for index, request in enumerate(malformed):
        actual = status(process, request)
        assert actual == 400, (index, actual)
    assert status(process, b'GET http://origin.proxrs.test:8080/ HTTP/1.1\r\nHost: x\r\nLarge: ' + b'x' * 32768 + b'\r\n\r\n') == 431
    started = time.monotonic()
    assert status(process, b'GET http://origin.proxrs.test:8080/ HTTP/1.1\r\nHost:') == 504
    assert 9 <= time.monotonic() - started < 13
    with closing(connection(process)) as client:
        client.request('GET', 'http://origin.proxrs.test:8080/truncated')
        response = client.getresponse()
        assert response.status == 200
        try:
            response.read()
            raise AssertionError('truncated origin was accepted as complete')
        except http.client.IncompleteRead as error:
            assert b'HTTP/1.1 502' not in error.partial
    print('PASS: malformed/oversized heads, 10s header 504 and failure after response headers')


def pipelined_errors(fixture, process):
    with socket.create_connection(('127.0.0.1', process.port), timeout=15) as stream:
        stream.sendall(b'GET http://origin.proxrs.test:8080/events HTTP/1.1\r\nHost: x\r\n\r\n'
                       b'POST http://origin.proxrs.test:8080/ HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\nTransfer-Encoding: chunked\r\n\r\n')
        wire = b''
        while part := stream.recv(4096):
            wire += part
        assert wire.startswith(b'HTTP/1.1 200 '), wire
        assert wire.index(b'data: second') < wire.index(b'HTTP/1.1 400 '), wire
        assert wire.count(b'HTTP/1.1 ') == 2, wire
    assert status(process, b'POST http://origin.proxrs.test:8080/upload HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\ninvalid-chunk\r\n') == 400
    assert status(process, b'GET http://origin.proxrs.test:8080/no-response HTTP/1.1\r\nHost: x\r\n\r\n'
                  b'POST http://origin.proxrs.test:8080/upload HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\ninvalid-chunk\r\n') == 502
    print('PASS: pipelined errors remain associated with their requests and malformed chunks return 400')


def diagnostics(fixture, process):
    assert status(process, b'GET http://origin.proxrs.test:1/ HTTP/1.1\r\nHost: x\r\n\r\n') == 502
    output = process.stderr
    assert 'destination connection failed' in output, output
    assert 'AllowTcpForwarding' not in output, output
    assert process.poll() is None
    config = fixture.docker('cat', '/etc/ssh/sshd_config')
    try:
        fixture.docker('sh', '-c', 'cat > /etc/ssh/sshd_config', input=config.replace(b'AllowTcpForwarding local', b'AllowTcpForwarding no'))
        fixture.docker('kill', '-HUP', '1')
        time.sleep(.3)
        with fixture.ready() as denied:
            for _ in range(2):
                assert status(denied, b'GET http://origin.proxrs.test:8080/ HTTP/1.1\r\nHost: x\r\n\r\n') == 502
            output = denied.stderr
            assert 'SSH forwarding denied' in output and 'AllowTcpForwarding' in output and 'PermitOpen' in output, output
            assert 'destination connection failed' not in output, output
            assert denied.poll() is None
            denied.terminate()
            assert denied.wait(timeout=5) == 0
    finally:
        fixture.docker('sh', '-c', 'cat > /etc/ssh/sshd_config', input=config)
        fixture.docker('kill', '-HUP', '1')
        time.sleep(.3)
    print('PASS: refused destinations and SSH policy denial have distinct diagnostics and preserve the SSH session')


def websocket(fixture, process):
    context = ssl.create_default_context(cafile=str(fixture.path / 'ca.pem'))
    with process.connect() as raw:
        with context.wrap_socket(raw, server_hostname='origin.proxrs.test') as stream:
            key = base64.b64encode(os.urandom(16)).decode()
            stream.sendall(f'GET /websocket HTTP/1.1\r\nHost: origin.proxrs.test\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n'.encode())
            head = b''
            while not head.endswith(b'\r\n\r\n'):
                head += stream.recv(1)
            expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
            assert head.startswith(b'HTTP/1.1 101 ') and expected in head
            reader = stream.makefile('rb')
            assert reader.read(7) == b'\x81\x05hello'
            mask = b'abcd'
            body = b'client-to-server'
            stream.sendall(bytes([0x81, 0x80 | len(body)]) + mask + bytes(value ^ mask[i % 4] for i, value in enumerate(body)))
            assert reader.read(2) == bytes([0x81, len(body)])
            assert reader.read(len(body)) == body
            reader.close()
    print('PASS: bidirectional WebSocket traffic inside certificate-validated TLS CONNECT')


def long_response_reuse(fixture, process):
    with closing(connection(process)) as client:
        client.request('GET', 'http://origin.proxrs.test:8080/long-response')
        response = client.getresponse()
        assert response.status == 200 and response.read() == b'firstfinal'
        original_socket = client.sock
        client.request('GET', 'http://origin.proxrs.test:8080/')
        response = client.getresponse()
        assert client.sock is original_socket and client.sock is not None
        assert response.status == 200, response.status
        assert response.read() == b'proxrs-docker-origin\n'
    print('PASS: a quiet response longer than 10s preserves same-connection reuse')


def connect_transitions(fixture, process):
    with process.connect('origin.proxrs.test:1', status=502) as failed:
        failed.settimeout(2)
        assert failed.recv(1) == b'', 'failed CONNECT must close rather than enter tunnel mode'
    with socket.create_connection(('127.0.0.1', process.port), timeout=5) as stream:
        stream.sendall(b'CONNECT origin.proxrs.test:9000 HTTP/1.1\r\nHost: origin.proxrs.test:9000\r\n\r\ncoalesced-data')
        # CONNECT has no HTTP body framing; read its header without buffering payload.
        head = b''
        while not head.endswith(b'\r\n\r\n'):
            head += stream.recv(1)
        assert head.startswith(b'HTTP/1.1 200 ')
        stream.shutdown(socket.SHUT_WR)
        data = b''
        while part := stream.recv(4096):
            data += part
        assert data == b'coalesced-dataafter-half-close', data
    with process.connect('origin.proxrs.test:9003') as stream:
        data = b''
        while part := stream.recv(4096):
            data += part
        assert data == b'destination-eof', data
        stream.sendall(b'client-after-eof')
        stream.shutdown(socket.SHUT_WR)
        deadline = time.monotonic() + 3
        while True:
            with closing(connection(process)) as probe:
                probe.request('GET', 'http://origin.proxrs.test:8080/half-closed')
                response = probe.getresponse()
                assert response.status == 200
                observed = response.read()
            if observed == b'client-after-eof':
                break
            assert time.monotonic() < deadline, ('destination-first EOF cut off client bytes', observed)
            time.sleep(.01)
    print('PASS: failed CONNECT closes; success preserves coalesced bytes and both half-close directions')


def echo_after_cleanup(process):
    deadline = time.monotonic() + 3
    while True:
        try:
            return process.connect('origin.proxrs.test:9000')
        except AssertionError as error:
            assert bytes(error.args[0]).startswith(b'HTTP/1.1 503 '), error
            assert time.monotonic() < deadline, 'canceled channel capacity did not recover within 3s'
            time.sleep(.01)


def limits(fixture, process):
    with ExitStack() as stack:
        tunnels = [stack.enter_context(closing(echo_after_cleanup(process))) for _ in range(128)]
        started = time.monotonic()
        assert status(process, b'GET http://origin.proxrs.test:8080/ HTTP/1.1\r\nHost: x\r\n\r\n') == 503
        assert time.monotonic() - started < 2
        idle = [stack.enter_context(closing(socket.create_connection(('127.0.0.1', process.port), timeout=2))) for _ in range(128)]
        with socket.create_connection(('127.0.0.1', process.port), timeout=2) as excess:
            try:
                assert excess.recv(1) == b''
            except ConnectionResetError:
                pass
        idle.pop().close()
        time.sleep(.1)
        assert status(process, b'GET / HTTP/1.1\r\nHost: x\r\n\r\n') == 400
        for stream in idle:
            stream.close()
        tunnels.pop().close()
        deadline = time.monotonic() + 3
        while True:
            actual = status(process, b'GET http://origin.proxrs.test:8080/ HTTP/1.1\r\nHost: x\r\n\r\n')
            if actual == 200:
                break
            assert actual == 503, actual
            assert time.monotonic() < deadline, 'released channel capacity did not recover'
            time.sleep(.02)
    # Pending opens count toward the same 128-channel bound.
    with ThreadPoolExecutor(max_workers=1) as pool, ExitStack() as stack:
        tunnels = [stack.enter_context(closing(echo_after_cleanup(process))) for _ in range(127)]
        def pending_open():
            deadline = time.monotonic() + 3
            while True:
                try:
                    process.connect('origin.proxrs.test:9001', status=504).close()
                    return
                except AssertionError as error:
                    assert bytes(error.args[0]).startswith(b'HTTP/1.1 503 '), error
                    assert time.monotonic() < deadline, 'previous channels did not finish cleanup'
                    time.sleep(.01)
        pending = pool.submit(pending_open)
        deadline = time.monotonic() + 3
        while True:
            actual = status(process, b'GET http://origin.proxrs.test:8080/ HTTP/1.1\r\nHost: x\r\n\r\n')
            if actual == 503:
                break
            assert actual == 200 and time.monotonic() < deadline, actual
            time.sleep(.02)
        assert not pending.done(), 'pending destination did not occupy a channel'

        tunnels.pop().close()
        time.sleep(.1)
        with closing(connection(process)) as client:
            client.request('GET', 'http://origin.proxrs.test:8080/')
            assert client.getresponse().status == 200
        pending.result(timeout=15)
    print('PASS: 128 channels including pending opens, 256 clients including tunnels, prompt excess and capacity recovery')


def cancellation(fixture, process):
    for _ in range(140):
        with closing(connection(process)) as client:
            client.request('GET', 'http://origin.proxrs.test:8080/bytes?count=1073741824')
            response = client.getresponse()
            assert response.status == 200
            assert response.read(1) == b'x'
            response.close()
    with closing(connection(process)) as client:
        client.request('GET', 'http://origin.proxrs.test:8080/')
        assert client.getresponse().read() == b'proxrs-docker-origin\n'
    print('PASS: 140 canceled large responses release SSH channels and keep healthy requests progressing')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', default='ubuntu:24.04')
    args = parser.parse_args()
    with Fixture(args.image) as fixture:
        with fixture.ready() as process:
            forwarding(fixture, process)
            authority_validation(fixture, process)
            framing(fixture, process)
            routing_and_headers(fixture, process)
            streaming(fixture, process)
            deadlines_and_errors(fixture, process)
            pipelined_errors(fixture, process)
            websocket(fixture, process)
            long_response_reuse(fixture, process)
            connect_transitions(fixture, process)
            cancellation(fixture, process)
            limits(fixture, process)
            diagnostics(fixture, process)
            logs = process.stdout + process.stderr
            for sensitive in ['end-to-end-value', 'must-not-forward', 'client-to-server', 'a' * 4096, 'b' * 4096]:
                assert sensitive not in logs, 'authorization or request body appeared in logs'
        print('PASS: authorization fields and request bodies remain absent from process logs')


if __name__ == '__main__':
    run_suite(main)
