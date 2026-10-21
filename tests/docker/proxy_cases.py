#!/usr/bin/env python3
"""Opt-in real-binary tests. No public origins or developer SSH configuration."""
import argparse
import concurrent.futures
import contextlib
import os
from pathlib import Path
import secrets
import signal
import socket
import socketserver
import threading
import ssl
import subprocess
import tempfile
import time
from fixture_record import record
from proxy_session import ProxySession, cleanup_all, defer_termination, run_suite

ROOT = Path(__file__).resolve().parents[2]

def command(*args, **kwargs):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs).stdout

class Fixture:
    def __init__(self, image):
        self.image = image
        self.container = None
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
            record("built", base_image=self.image, image_id=built_image)
            with defer_termination():
                self.container = command("docker", "run", "--detach", "--rm", "--label", "dev.proxrs.fixture=cli",
                                         "--publish", "127.0.0.1::22", "--add-host", "origin.proxrs.test:127.0.0.1", built_image).decode().strip()
                record("created", container_id=self.container, image_id=built_image)
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
        return [os.environ.get("PROXRS_TEST_BINARY", str(ROOT / "target/debug/proxrs")), "up", "--host", "127.0.0.1", "--user", "proxrs",
                "--ssh-port", str(self.port), "--port", "0", "--password-stdin", "--known-hosts",
                str(known_hosts or self.path / "known_hosts")]

    def start(self, args=None, password=None, *, env=None, input_suffix=b''):
        return ProxySession.pipe(args or self.args(), env=env,
                                 password_input=((password if password is not None else self.password) + "\r\n").encode() + input_suffix,
                                 secrets=self.output_secrets())

    def terminal(self, args=None, password_pipe=False):
        return ProxySession.terminal(args or self.args(),
                                     password_input=(self.password + "\n").encode() if password_pipe else None,
                                     secrets=self.output_secrets())

    def output_secrets(self):
        return (self.password, 'proxrs-sensitive-header-marker', 'opaque tunnel bytes',
                'end-to-end-value', 'must-not-forward', 'client-to-server', 'a' * 4096, 'b' * 4096)

    @contextlib.contextmanager
    def ready(self, args=None):
        with self.start(args) as session:
            session.ready()
            assert session.stdout.count("http://127.0.0.1:") == 4, session.stdout
            process_command = command("ps", "-p", str(session.pid), "-o", "command=").decode()
            assert self.password not in process_command
            yield session

    def https(self, session, authority="origin.proxrs.test:8443"):
        context = ssl.create_default_context(cafile=str(self.path / "ca.pem"))
        with session.connect(authority) as raw:
            with context.wrap_socket(raw, server_hostname="origin.proxrs.test") as stream:
                stream.sendall(b"GET / HTTP/1.1\r\nHost: origin.proxrs.test\r\nConnection: close\r\n\r\n")
                response = b""
                while data := stream.recv(4096):
                    response += data
                assert response.endswith(b"proxrs-docker-origin\n"), response

    @defer_termination()
    def __exit__(self, *args):
        def remove_container():
            if self.container:
                command("docker", "rm", "--force", self.container, timeout=15)
                record("removed", container_id=self.container)
        cleanup_all([('container', remove_container), ('credentials', self.directory.cleanup)])

def expect_startup_failure(fixture, expected, args=None, password=None, limit=14):
    started = time.monotonic()
    with fixture.start(args=args, password=password) as process:
        assert process.wait(timeout=limit) != 0
        elapsed = time.monotonic() - started
        assert not process.stdout, "failed startup printed exports"
        diagnostic = process.stderr
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
        elapsed = expect_startup_failure(fixture, "startup deadline", limit=54)
        assert 46 <= elapsed <= 53, elapsed
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
        with fixture.ready() as process:
            fixture.https(process)
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
        with fixture.ready() as process:
            process.connect(status=502).close()
            process.connect(status=502).close()
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
            with fixture.ready(args) as process:
                fixture.https(process)
                process.terminate()
                assert process.wait(timeout=5) == 0
        finally:
            server.shutdown()
            thread.join(timeout=2)
    print("PASS: IPv6 SSH address through byte-for-byte loopback relay to the real fixture")

def sigint_case(fixture):
    with fixture.ready() as process:
        with process.connect("127.0.0.1:9000") as stream:
            process.send_signal(signal.SIGINT)
            assert process.wait(timeout=5) == 0
            assert stream.recv(1) == b""
        print("PASS: SIGINT closes active CONNECT and exits cleanly")

def ssh_loss_case(fixture):
    with fixture.ready() as process:
        stream = process.connect("127.0.0.1:9000")
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
            assert process.poll() is None
            try:
                assert stream.recv(100) == b"", "failed tunnel injected application bytes"
            except ConnectionResetError:
                pass
            assert "SSH connection lost" in process.stderr
            with process.connect(status=503):
                pass
            deadline = time.monotonic() + 6
            while True:
                try:
                    fixture.https(process)
                    break
                except (AssertionError, OSError):
                    assert time.monotonic() < deadline, "SSH did not recover"
                    time.sleep(.05)
            process.terminate()
            assert process.wait(timeout=5) == 0
            with socket.socket() as probe:
                assert probe.connect_ex(("127.0.0.1", process.port)) != 0, "listener survived shutdown"
        finally:
            stream.close()
        print("PASS: real SSH loss closes active tunnel, returns503, recovers, then cleans listener on shutdown")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="ubuntu:24.04")
    args = parser.parse_args()
    with Fixture(args.image) as fixture:
        authentication_cases(fixture)
        rsa_host_key_case(fixture)
        forwarding_denied_case(fixture)
        ipv6_ssh_case(fixture)
        with fixture.ready() as process:
            listeners = command("lsof", "-nP", "-a", "-p", str(process.pid), "-iTCP", "-sTCP:LISTEN").decode()
            assert f"TCP 127.0.0.1:{process.port} (LISTEN)" in listeners, listeners
            assert f"*:{process.port}" not in listeners, listeners
            fixture.https(process)
            fixture.https(process, "[::1]:8443")
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda _: fixture.https(process), range(16)))
            with process.connect("127.0.0.1:9000") as stream:
                stream.sendall(b"opaque tunnel bytes")
                stream.shutdown(socket.SHUT_WR)
                response = b""
                while data := stream.recv(4096):
                    response += data
                assert response == b"opaque tunnel bytesafter-half-close", response
            for authority in ["missing-port", "host:0", "host:65536", "host:abc", "user@host:80", "[broken:ipv6]:80", "http://origin.proxrs.test:8443/"]:
                process.connect(authority, status=400).close()
            process.connect("origin.proxrs.test:65535", status=502).close()
            process.connect("origin.proxrs.test:1", status=502).close()
            fixture.https(process)
            # Slow readers must not stall healthy channels or grow without bound.
            stalled_readers = [process.connect("127.0.0.1:9002") for _ in range(4)]
            try:
                for reader in stalled_readers:
                    reader.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
                    reader.sendall(b"x")
                time.sleep(1)
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                    list(pool.map(lambda _: fixture.https(process), range(16)))
            finally:
                for reader in stalled_readers:
                    reader.close()
            # A stalled open must not block other destinations, even after its504.
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                started = time.monotonic()
                pending = pool.submit(process.connect, "127.0.0.1:9001", 504)
                time.sleep(.2)
                fixture.https(process)
                assert time.monotonic() - started < 3
                pending.result(timeout=14).close()
                assert 9 <= time.monotonic() - started <= 13
            fixture.https(process)
            process.terminate()
            assert process.wait(timeout=5) == 0
            with contextlib.suppress(ConnectionRefusedError):
                with socket.create_connection(("127.0.0.1", process.port), timeout=1):
                    raise AssertionError("listener survived shutdown")
            print(f"PASS {args.image}: HTTPS, remote DNS, IPv6, concurrency, half-close, CONNECT failures, exports, destination deadline and shutdown")
            sigint_case(fixture)
            ssh_loss_case(fixture)

if __name__ == "__main__":
    run_suite(main)
