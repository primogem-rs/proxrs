#!/usr/bin/env python3
"""Opt-in real-binary tests. No public origins or developer SSH configuration."""
import argparse
import concurrent.futures
import contextlib
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import socketserver
import threading
import ssl
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]

def command(*args, **kwargs):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs).stdout

class Fixture:
    def __init__(self, image):
        self.image = image
        self.container = None
        self.processes = []
        self.directory = tempfile.TemporaryDirectory(prefix="proxrs-cli-")
        self.path = Path(self.directory.name)
        self.password = "  " + secrets.token_urlsafe(32) + "  "

    def __enter__(self):
        try:
            tag = "proxrs-test-" + self.image.replace(":", "-")
            image_id = self.path / "image-id"
            command("docker", "build", "--iidfile", str(image_id), "--build-arg", "BASE_IMAGE=" + self.image,
                    "--tag", tag, str(ROOT / "tests/docker"))
            built_image = image_id.read_text().strip()
            self.container = command("docker", "run", "--detach", "--rm", "--label", "dev.proxrs.fixture=cli",
                                     "--publish", "127.0.0.1::22", "--add-host", "origin.proxrs.test:127.0.0.1", built_image).decode().strip()
            self.port = int(command("docker", "port", self.container, "22/tcp").decode().strip().rsplit(":", 1)[1])
            self.docker("chpasswd", input=("proxrs:" + self.password + "\n").encode())
            for _ in range(100):
                try:
                    self.docker("test", "-s", "/run/sshd.pid")
                    self.key = self.docker("cat", "/etc/ssh/ssh_host_ed25519_key.pub").decode().strip()
                    (self.path / "known_hosts").write_text(f"[127.0.0.1]:{self.port} {self.key}\n")
                    (self.path / "ca.pem").write_bytes(self.docker("cat", "/run/test-ca.pem"))
                    return self
                except subprocess.CalledProcessError:
                    time.sleep(.1)
            raise AssertionError("fixture failed to become ready")
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def docker(self, *args, **kwargs):
        return command("docker", "exec", "-i", self.container, *args, **kwargs)

    def args(self, known_hosts=None):
        return [str(ROOT / "target/debug/proxrs"), "up", "--host", "127.0.0.1", "--user", "proxrs",
                "--ssh-port", str(self.port), "--port", "0", "--password-stdin", "--known-hosts",
                str(known_hosts or self.path / "known_hosts")]

    def start(self, args=None, password=None):
        number = len(self.processes)
        out = open(self.path / f"stdout-{number}", "w+")
        err = open(self.path / f"stderr-{number}", "w+")
        process = subprocess.Popen(args or self.args(), stdin=subprocess.PIPE, stdout=out, stderr=err, start_new_session=True)
        self.processes.append((process, out, err))
        process.stdin.write(((password if password is not None else self.password) + "\r\n").encode())
        process.stdin.close()
        return process, out, err

    def ready(self, args=None):
        process, out, err = self.start(args)
        for _ in range(150):
            out.seek(0)
            exports = out.read()
            match = re.search(r"export HTTP_PROXY='http://127.0.0.1:(\d+)'", exports)
            if match and "export no_proxy=" in exports:
                self.proxy_port = int(match[1])
                assert exports.count("http://127.0.0.1:") == 4, exports
                assert "export NO_PROXY='localhost,127.0.0.1,::1'" in exports
                process_command = command("ps", "-p", str(process.pid), "-o", "command=").decode()
                assert self.password not in process_command
                return process
            if process.poll() is not None:
                err.seek(0)
                raise AssertionError("proxy exited before readiness: " + err.read())
            time.sleep(.1)
        raise AssertionError("proxy did not become ready")

    def connect(self, authority="origin.proxrs.test:8443", status=200):
        stream = socket.create_connection(("127.0.0.1", self.proxy_port), timeout=15)
        stream.sendall(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\nProxy-Authorization: Basic proxrs-sensitive-header-marker\r\n\r\n".encode())
        response = b""
        while b"\r\n\r\n" not in response:
            data = stream.recv(1)
            if not data:
                break
            response += data
        assert response.startswith(f"HTTP/1.1 {status} ".encode()), response
        return stream

    def https(self, authority="origin.proxrs.test:8443"):
        context = ssl.create_default_context(cafile=str(self.path / "ca.pem"))
        with self.connect(authority) as raw:
            with context.wrap_socket(raw, server_hostname="origin.proxrs.test") as stream:
                stream.sendall(b"GET / HTTP/1.1\r\nHost: origin.proxrs.test\r\nConnection: close\r\n\r\n")
                response = b""
                while data := stream.recv(4096):
                    response += data
                assert response.endswith(b"proxrs-docker-origin\n"), response

    def __exit__(self, *args):
        failures = []
        try:
            for process, out, err in self.processes:
                try:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=8)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                    out.seek(0)
                    err.seek(0)
                    output = out.read() + err.read()
                    if self.password in output:
                        failures.append("password leaked")
                    if "proxrs-sensitive-header-marker" in output or "opaque tunnel bytes" in output:
                        failures.append("request headers or payload leaked")
                except Exception as error:
                    failures.append(str(error))
                finally:
                    out.close()
                    err.close()
        finally:
            try:
                if self.container:
                    command("docker", "rm", "--force", self.container)
            finally:
                self.directory.cleanup()
        assert not failures, "; ".join(failures)

def expect_startup_failure(fixture, expected, args=None, password=None, limit=14):
    started = time.monotonic()
    process, out, err = fixture.start(args=args, password=password)
    assert process.wait(timeout=limit) != 0
    elapsed = time.monotonic() - started
    out.seek(0)
    err.seek(0)
    assert not out.read(), "failed startup printed exports"
    diagnostic = err.read()
    assert expected in diagnostic, diagnostic
    assert fixture.password not in diagnostic
    return elapsed

def authentication_cases(fixture):
    expect_startup_failure(fixture, "authentication rejected", password="incorrect-disposable-password")
    original = (fixture.path / "known_hosts").read_text()
    alternate = fixture.path / "alternate"
    command("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(alternate))
    other_key = alternate.with_suffix(".pub").read_text().strip()
    cases = [
        ("", "unknown SSH host key"),
        (f"[127.0.0.1]:{fixture.port} {other_key}\n", "host key changed"),
        ("@revoked " + original, "host key is revoked"),
        ("@cert-authority " + original, "authority trust is unsupported"),
        ("broken storage\n", "malformed known-hosts storage"),
    ]
    try:
        for content, expected in cases:
            (fixture.path / "known_hosts").write_text(content)
            expect_startup_failure(fixture, expected)
        expect_startup_failure(fixture, "could not read known-hosts storage", args=fixture.args(fixture.path))
        (fixture.path / "known_hosts").chmod(0)
        try:
            expect_startup_failure(fixture, "could not read known-hosts storage")
        finally:
            (fixture.path / "known_hosts").chmod(0o600)
    finally:
        (fixture.path / "known_hosts").write_text(original)
    pam = fixture.docker("cat", "/etc/pam.d/sshd")
    try:
        fixture.docker("sh", "-c", "cat > /etc/pam.d/sshd", input=b"auth optional pam_exec.so /bin/sleep 30\n" + pam)
        elapsed = expect_startup_failure(fixture, "startup deadline", limit=14)
        assert 9 <= elapsed <= 13, elapsed
    finally:
        fixture.docker("sh", "-c", "cat > /etc/pam.d/sshd", input=pam)
    config = fixture.docker("cat", "/etc/ssh/sshd_config")
    try:
        fixture.docker("sh", "-c", "cat > /etc/ssh/sshd_config", input=config.replace(b"PasswordAuthentication yes", b"PasswordAuthentication no").replace(b"KbdInteractiveAuthentication no", b"KbdInteractiveAuthentication yes"))
        fixture.docker("kill", "-HUP", "1")
        time.sleep(.3)
        expect_startup_failure(fixture, "unsupported authentication")
    finally:
        fixture.docker("sh", "-c", "cat > /etc/ssh/sshd_config", input=config)
        fixture.docker("kill", "-HUP", "1")
        time.sleep(.3)
    print("PASS: trust rejection matrix, incorrect password, unsupported auth, stalled authentication deadline")

def rsa_host_key_case(fixture):
    config = fixture.docker("cat", "/etc/ssh/sshd_config")
    trust = (fixture.path / "known_hosts").read_text()
    try:
        rsa_key = fixture.docker("cat", "/etc/ssh/ssh_host_rsa_key.pub").decode().strip()
        (fixture.path / "known_hosts").write_text(f"[127.0.0.1]:{fixture.port} {rsa_key}\n")
        fixture.docker("sh", "-c", "cat > /etc/ssh/sshd_config", input=config.replace(b"ssh_host_ed25519_key", b"ssh_host_rsa_key"))
        fixture.docker("kill", "-HUP", "1")
        time.sleep(.3)
        process = fixture.ready()
        fixture.https()
        process.terminate()
        assert process.wait(timeout=5) == 0
    finally:
        (fixture.path / "known_hosts").write_text(trust)
        fixture.docker("sh", "-c", "cat > /etc/ssh/sshd_config", input=config)
        fixture.docker("kill", "-HUP", "1")
        time.sleep(.3)
    print("PASS: RSA-only SSH host key with RSA/SHA2 verification")

def forwarding_denied_case(fixture):
    config = fixture.docker("cat", "/etc/ssh/sshd_config")
    try:
        fixture.docker("sh", "-c", "cat > /etc/ssh/sshd_config", input=config.replace(b"AllowTcpForwarding local", b"AllowTcpForwarding no"))
        fixture.docker("kill", "-HUP", "1")
        time.sleep(.3)
        process = fixture.ready()
        fixture.connect(status=502).close()
        fixture.connect(status=502).close()
        assert process.poll() is None
        process.terminate()
        assert process.wait(timeout=5) == 0
    finally:
        fixture.docker("sh", "-c", "cat > /etc/ssh/sshd_config", input=config)
        fixture.docker("kill", "-HUP", "1")
        time.sleep(.3)
    print("PASS: forwarding denial is a request failure and keeps SSH session alive")

def ipv6_ssh_case(fixture):
    class Relay(socketserver.BaseRequestHandler):
        def handle(self):
            with socket.create_connection(("127.0.0.1", fixture.port)) as remote:
                def copy(source, destination):
                    try:
                        while data := source.recv(16384):
                            destination.sendall(data)
                        destination.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                thread = threading.Thread(target=copy, args=(self.request, remote), daemon=True)
                thread.start()
                copy(remote, self.request)
                thread.join(timeout=2)
    class Server(socketserver.ThreadingTCPServer):
        address_family = socket.AF_INET6
        daemon_threads = True
    with Server(("::1", 0), Relay) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            trust = fixture.path / "ipv6_known_hosts"
            trust.write_text(f"[::1]:{port} {fixture.key}\n")
            args = fixture.args(trust)
            args[args.index("--host") + 1] = "::1"
            args[args.index("--ssh-port") + 1] = str(port)
            process = fixture.ready(args)
            fixture.https()
            process.terminate()
            assert process.wait(timeout=5) == 0
        finally:
            server.shutdown()
            thread.join(timeout=2)
    print("PASS: IPv6 SSH address through byte-for-byte loopback relay to the real fixture")

def sigint_case(fixture):
    process = fixture.ready()
    with fixture.connect("127.0.0.1:9000") as stream:
        process.send_signal(signal.SIGINT)
        assert process.wait(timeout=5) == 0
        assert stream.recv(1) == b""
    print("PASS: SIGINT closes active CONNECT and exits cleanly")

def ssh_loss_case(fixture):
    process = fixture.ready()
    stream = fixture.connect("127.0.0.1:9000")
    try:
        stream.sendall(b"before-outage")
        assert stream.recv(100) == b"before-outage"
        fixture.docker("python3", "-c", """import os, signal
from pathlib import Path
for path in Path('/proc').iterdir():
    if path.name.isdigit() and path.name != '1':
        try:
            if (path / 'comm').read_text().strip().startswith('sshd'):
                os.kill(int(path.name), signal.SIGKILL)
        except (FileNotFoundError, ProcessLookupError):
            pass
""")
        assert process.wait(timeout=5) != 0
        try:
            assert stream.recv(100) == b"", "failed tunnel injected application bytes"
        except ConnectionResetError:
            pass
        fixture.processes[-1][2].seek(0)
        assert "SSH connection lost" in fixture.processes[-1][2].read()
        with contextlib.suppress(ConnectionRefusedError):
            with socket.create_connection(("127.0.0.1", fixture.proxy_port), timeout=1):
                raise AssertionError("listener survived SSH loss")
    finally:
        stream.close()
    print("PASS: real SSH loss closes active tunnel/listener and exits nonzero")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="ubuntu:24.04")
    args = parser.parse_args()
    with Fixture(args.image) as fixture:
        authentication_cases(fixture)
        rsa_host_key_case(fixture)
        forwarding_denied_case(fixture)
        ipv6_ssh_case(fixture)
        process = fixture.ready()
        listeners = command("lsof", "-nP", "-a", "-p", str(process.pid), "-iTCP", "-sTCP:LISTEN").decode()
        assert f"TCP 127.0.0.1:{fixture.proxy_port} (LISTEN)" in listeners, listeners
        assert f"*:{fixture.proxy_port}" not in listeners, listeners
        fixture.https()
        fixture.https("[::1]:8443")
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: fixture.https(), range(16)))
        with fixture.connect("127.0.0.1:9000") as stream:
            stream.sendall(b"opaque tunnel bytes")
            stream.shutdown(socket.SHUT_WR)
            response = b""
            while data := stream.recv(4096):
                response += data
            assert response == b"opaque tunnel bytesafter-half-close", response
        for authority in ["missing-port", "host:0", "host:65536", "host:abc", "user@host:80", "[broken:ipv6]:80", "http://origin.proxrs.test:8443/"]:
            fixture.connect(authority, status=400).close()
        fixture.connect("origin.proxrs.test:65535", status=502).close()
        fixture.connect("origin.proxrs.test:1", status=502).close()
        fixture.https()
        # Slow readers must not stall healthy channels or grow without bound.
        stalled_readers = [fixture.connect("127.0.0.1:9002") for _ in range(4)]
        try:
            for reader in stalled_readers:
                reader.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
                reader.sendall(b"x")
            time.sleep(1)
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda _: fixture.https(), range(16)))
        finally:
            for reader in stalled_readers:
                reader.close()
        # A stalled open must not block other destinations, even after its504.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            started = time.monotonic()
            pending = pool.submit(fixture.connect, "127.0.0.1:9001", 504)
            time.sleep(.2)
            fixture.https()
            assert time.monotonic() - started < 3
            pending.result(timeout=14).close()
            assert 9 <= time.monotonic() - started <= 13
        fixture.https()
        process.terminate()
        assert process.wait(timeout=5) == 0
        with contextlib.suppress(ConnectionRefusedError):
            with socket.create_connection(("127.0.0.1", fixture.proxy_port), timeout=1):
                raise AssertionError("listener survived shutdown")
        print(f"PASS {args.image}: HTTPS, remote DNS, IPv6, concurrency, half-close, CONNECT failures, exports, destination deadline and shutdown")
        sigint_case(fixture)
        ssh_loss_case(fixture)

if __name__ == "__main__":
    main()
