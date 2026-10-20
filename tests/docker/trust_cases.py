#!/usr/bin/env python3
"""Real CLI trust and terminal acceptance, opt-in and isolated per fixture."""
import argparse
import contextlib
import fcntl
import os
import pty
import re
import select
import shlex
import signal
import termios
import time

from proxy_cases import Fixture, command, expect_startup_failure


class TerminalProcess:
    def __init__(self, fixture, args=None, password_pipe=False):
        self.fixture = fixture
        self.output = bytearray()
        self.status = None
        self.out = open(fixture.path / f"pty-output-{time.monotonic_ns()}", "w+")
        self.master, slave = pty.openpty()
        self.before = termios.tcgetattr(slave)
        read_pipe, write_pipe = os.pipe() if password_pipe else (None, None)
        self.pid = os.fork()
        if self.pid == 0:
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            os.dup2(read_pipe if password_pipe else slave, 0)
            os.dup2(self.out.fileno(), 1)
            os.dup2(slave, 2)
            os.close(self.master)
            if password_pipe:
                os.close(write_pipe)
                os.close(read_pipe)
            if slave > 2:
                os.close(slave)
            os.execv((args or fixture.args())[0], args or fixture.args())
        os.close(slave)
        if password_pipe:
            os.close(read_pipe)
            os.write(write_pipe, (fixture.password + "\n").encode())
            os.close(write_pipe)

    def read(self, timeout=.05):
        if select.select([self.master], [], [], timeout)[0]:
            try:
                self.output.extend(os.read(self.master, 65536))
            except OSError:
                pass
        assert self.fixture.password.encode() not in self.output, "password appeared on terminal"
        return self.output.decode(errors="replace")

    def wait_text(self, text, timeout=15):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            output = self.read()
            if text in output:
                return output
            if self.poll() is not None:
                raise AssertionError("process exited before prompt: " + output)
        raise AssertionError("missing prompt: " + text + ": " + self.read(0))

    def send(self, text):
        os.write(self.master, text.encode())

    def poll(self):
        if self.status is None:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.status = os.waitstatus_to_exitcode(status)
        return self.status

    def wait(self, timeout=5):
        end = time.monotonic() + timeout
        while self.poll() is None:
            self.read()
            if time.monotonic() >= end:
                raise AssertionError("terminal process did not exit: " + self.read(0))
        self.read(0)
        return self.status

    def ready(self, timeout=15):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.out.seek(0)
            output = self.out.read()
            self.read(0)
            assert self.fixture.password not in output
            match = re.search(r"export HTTP_PROXY='http://127.0.0.1:(\d+)'", output)
            if match and "export no_proxy=" in output:
                self.fixture.proxy_port = int(match[1])
                return
            if self.poll() is not None:
                raise AssertionError("process exited before readiness: " + self.read(0))
            time.sleep(.03)
        raise AssertionError("terminal process was not ready: " + self.read(0))

    def restored(self):
        assert termios.tcgetattr(self.master) == self.before, "terminal settings were not restored"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        try:
            # The PTY helper owns this fresh session/process group, including
            # the waiting shell used to verify hand-back of terminal input.
            try:
                os.killpg(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if self.poll() is None:
                try:
                    self.wait()
                except BaseException:
                    os.killpg(self.pid, signal.SIGKILL)
                    os.waitpid(self.pid, 0)
                    raise
            self.read(0)
        finally:
            os.close(self.master)
            self.out.close()


def hidden_password(fixture):
    args = fixture.args()
    args.remove("--password-stdin")
    with TerminalProcess(fixture, args) as process:
        process.wait_text("SSH password:")
        assert not termios.tcgetattr(process.master)[3] & (termios.ECHO | termios.ECHONL)
        process.send(fixture.password + "\n")
        process.ready()
        process.restored()
        fixture.https()
        os.kill(process.pid, signal.SIGTERM)
        assert process.wait() == 0
        process.restored()
    print("PASS: hidden terminal password, HTTPS, and terminal restoration")


def enrollment(fixture):
    store = fixture.path / "enrolled_hosts"
    preserved = f"unrelated.example {fixture.key}\n# unrelated trust is preserved without a trailing newline"
    store.write_text(preserved)
    args = fixture.args(store)
    with TerminalProcess(fixture, args, password_pipe=True) as process:
        output = process.wait_text("Trust this key and save it? [yes/NO]")
        key_file = fixture.path / "public-key"
        key_file.write_text(fixture.key + "\n")
        fingerprint = command("ssh-keygen", "-lf", str(key_file), "-E", "sha256").decode().split()[1]
        assert fingerprint in output
        assert "127.0.0.1" in output and str(fixture.port) in output and "ssh-ed25519" in output
        assert not store.read_text().endswith("\n")
        process.send("yes\n")
        process.ready()
        fixture.https()
        os.kill(process.pid, signal.SIGTERM)
        assert process.wait() == 0
        process.restored()
    contents = store.read_text()
    assert contents.startswith(preserved + "\n")
    assert f"[127.0.0.1]:{fixture.port} " in contents
    process = fixture.ready(args)
    fixture.https()
    process.terminate()
    assert process.wait(timeout=5) == 0
    print("PASS: fingerprint approval through terminal, piped password, preserved storage, trust reuse")


def hashed_trust(fixture):
    store = fixture.path / "hashed_hosts"
    store.write_text((fixture.path / "known_hosts").read_text())
    command("ssh-keygen", "-q", "-H", "-f", str(store))
    assert store.read_text().startswith("|1|")
    process = fixture.ready(fixture.args(store))
    fixture.https()
    process.terminate()
    assert process.wait(timeout=5) == 0
    print("PASS: OpenSSH-generated hashed nondefault-port host trust")


def patterned_trust(fixture):
    store = fixture.path / "pattern_hosts"
    store.write_text(f"!unrelated,?[127.0.0.1]:*,[127.0.0.?]:* {fixture.key}\n")
    process = fixture.ready(fixture.args(store))
    fixture.https()
    process.terminate()
    assert process.wait(timeout=5) == 0
    print("PASS: ordinary wildcard, question-mark, and negative host patterns")


def whitespace_trust(fixture):
    store = fixture.path / "spaced_hosts"
    store.write_text(f"  # comment before a normal tab-separated entry\n\t[127.0.0.1]:{fixture.port}\t  {fixture.key}\n")
    process = fixture.ready(fixture.args(store))
    fixture.https()
    process.terminate()
    assert process.wait(timeout=5) == 0
    print("PASS: ordinary known-host whitespace and comments")


def certificate_only_server(fixture):
    original = fixture.docker("cat", "/etc/ssh/sshd_config")
    fixture.docker("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", "/run/host-ca")
    fixture.docker("ssh-keygen", "-s", "/run/host-ca", "-I", "proxrs-fixture", "-h", "-n", "127.0.0.1", "/etc/ssh/ssh_host_ed25519_key.pub")
    try:
        config = original + b"\nHostCertificate /etc/ssh/ssh_host_ed25519_key-cert.pub\nHostKeyAlgorithms ssh-ed25519-cert-v01@openssh.com\n"
        fixture.docker("sh", "-c", "cat > /etc/ssh/sshd_config", input=config)
        fixture.docker("kill", "-HUP", "1")
        time.sleep(.3)
        expect_startup_failure(fixture, "host certificates are unsupported")
    finally:
        fixture.docker("sh", "-c", "cat > /etc/ssh/sshd_config", input=original)
        fixture.docker("kill", "-HUP", "1")
        time.sleep(.3)
    print("PASS: actual certificate-only SSH server fails clearly before authentication")


@contextlib.contextmanager
def observed_authentication(fixture):
    pam = fixture.docker("cat", "/etc/pam.d/sshd")
    fixture.docker("sh", "-c", "cat > /usr/local/bin/proxrs-count-auth", input=b"#!/bin/sh\nprintf x >> /run/proxrs-auth-attempts\n")
    fixture.docker("chmod", "755", "/usr/local/bin/proxrs-count-auth")
    fixture.docker("touch", "/run/proxrs-auth-attempts")
    try:
        fixture.docker("sh", "-c", "cat > /etc/pam.d/sshd", input=b"auth optional pam_exec.so /usr/local/bin/proxrs-count-auth\n" + pam)
        yield
    finally:
        fixture.docker("sh", "-c", "cat > /etc/pam.d/sshd", input=pam)


def authentication_count(fixture):
    return len(fixture.docker("cat", "/run/proxrs-auth-attempts"))


def trust_matrix(fixture):
    store = fixture.path / "matrix_hosts"
    original = (fixture.path / "known_hosts").read_text()
    other = fixture.docker("cat", "/etc/ssh/ssh_host_rsa_key.pub").decode().strip()
    other_record = f"[127.0.0.1]:{fixture.port} {other}\n"
    allowed = [
        other_record + original,
        original + other_record,
        "@revoked " + other_record + original,
        original + f"@cert-authority unrelated.example {other}\n",
        f"!unrelated,* {fixture.key}\n",
        f"![127.0.0.1]:{fixture.port},* {other}\n" + original,
    ]
    for content in allowed:
        store.write_text(content)
        process = fixture.ready(fixture.args(store))
        fixture.https()
        process.terminate()
        assert process.wait(timeout=5) == 0
    rejected = [
        (original + "@revoked " + original, "host key is revoked"),
        ("@revoked " + original + original, "host key is revoked"),
        (original + "@cert-authority " + original, "authority trust is unsupported"),
        (other_record, "host key changed"),
        (f"![127.0.0.1]:{fixture.port},* {fixture.key}\n", "unknown SSH host key"),
        (f"!unrelated {fixture.key}\n", "unknown SSH host key"),
        (f"! {fixture.key}\n", "malformed known-hosts"),
        (f"![127.0.0.1]:{fixture.port},|invalid {fixture.key}\n", "malformed known-hosts"),
        (f"@unsupported [127.0.0.1]:{fixture.port} {fixture.key}\n", "malformed known-hosts"),
        (f"[127.0.0.1]:{fixture.port} ssh-ed25519 INVALID\n", "malformed known-hosts"),
        (f"|1|AA==|AAAAAAAAAAAAAAAAAAAAAAAAAAA= {fixture.key}\n", "malformed known-hosts"),
        (f"|1|AAAAAAAAAAAAAAAAAAAAAAAAAAA=|AA== {fixture.key}\n", "malformed known-hosts"),
        (f"|2|bad|bad {fixture.key}\n", "malformed known-hosts"),
        (original + "unrelated malformed\n", "malformed known-hosts"),
    ]
    for content, expected in rejected:
        store.write_text(content)
        before = authentication_count(fixture)
        expect_startup_failure(fixture, expected, args=fixture.args(store))
        assert authentication_count(fixture) == before, "rejected trust reached password authentication"
        assert store.read_text() == content
    before = authentication_count(fixture)
    expect_startup_failure(fixture, "could not read known-hosts storage", args=fixture.args(fixture.path))
    store.chmod(0)
    try:
        expect_startup_failure(fixture, "could not read known-hosts storage", args=fixture.args(store))
    finally:
        store.chmod(0o600)
    assert authentication_count(fixture) == before
    print("PASS: multiple-key order, revocations, negative patterns, CA applicability, changed keys, malformed/unreadable storage before authentication")


def refusal_and_persistence(fixture):
    for reply in ("\n", "no\n"):
        store = fixture.path / f"refused-{len(reply)}"
        before = authentication_count(fixture)
        with TerminalProcess(fixture, fixture.args(store), password_pipe=True) as process:
            process.wait_text("Trust this key and save it? [yes/NO]")
            process.send(reply)
            assert process.wait() != 0
            assert "approval refused" in process.read(0)
            process.restored()
        assert not store.exists()
        assert authentication_count(fixture) == before
    store = fixture.path / "readonly_hosts"
    store.write_text("# untouched\n")
    store.chmod(0o400)
    before = authentication_count(fixture)
    try:
        with TerminalProcess(fixture, fixture.args(store), password_pipe=True) as process:
            process.wait_text("Trust this key and save it? [yes/NO]")
            process.send("yes\n")
            assert process.wait() != 0
            assert "could not save approved SSH host key" in process.read(0)
            process.restored()
        assert authentication_count(fixture) == before
        assert store.read_text() == "# untouched\n"
    finally:
        store.chmod(0o600)
    print("PASS: blank/declined approval and persistence failure prevent authentication")


def noninteractive_password(fixture):
    args = fixture.args()
    args.remove("--password-stdin")
    before = authentication_count(fixture)
    expect_startup_failure(fixture, "use --password-stdin", args=args)
    assert authentication_count(fixture) == before
    print("PASS: no controlling terminal directs to --password-stdin before password authentication")


def terminal_cancellation(fixture):
    for prompt in ("password", "approval"):
        for stopping in (signal.SIGTERM, signal.SIGINT):
            args = fixture.args(fixture.path / f"cancel-{stopping}" if prompt == "approval" else None)
            if prompt == "password":
                args.remove("--password-stdin")
            before = authentication_count(fixture)
            with TerminalProcess(fixture, args, password_pipe=prompt == "approval") as process:
                process.wait_text("SSH password:" if prompt == "password" else "Trust this key and save it? [yes/NO]")
                if stopping == signal.SIGINT:
                    process.send("\x03")
                else:
                    os.kill(process.pid, stopping)
                assert process.wait(timeout=3) == 0
                process.restored()
            assert authentication_count(fixture) == before
    args = fixture.args()
    args.remove("--password-stdin")
    with TerminalProcess(fixture, args) as process:
        process.wait_text("SSH password:")
        process.send("\x04")
        assert process.wait() != 0
        process.restored()
    print("PASS: Ctrl-C/SIGTERM cancel both prompts without newline; EOF restores terminal state")


def terminal_input_lifetime(fixture):
    args = fixture.args()
    args.remove("--password-stdin")
    pid_file = fixture.path / "terminal-child.pid"
    # Keep the controlling session alive after proxrs exits, as a real shell
    # does. Exiting a session leader would otherwise flush the PTY for us.
    script = (shlex.join(args) + " & child=$!; printf '%s' \"$child\" > "
              + shlex.quote(str(pid_file))
              + "; wait \"$child\"; printf 'child-exited\\n' >&2; "
              + "IFS= read -r next; test \"$next\" = next-command")
    for completed in (False, True):
        with TerminalProcess(fixture, ["/bin/sh", "-c", script]) as process:
            process.wait_text("SSH password:")
            child = int(pid_file.read_text())
            try:
                process.send(fixture.password + ("\nnext-command\n" if completed else ""))
                if completed:
                    process.ready()
                    fixture.https()
                else:
                    time.sleep(.1)
                os.kill(child, signal.SIGTERM)
                process.wait_text("child-exited")
                process.restored()
                if not completed:
                    process.send("next-command\n")
                assert process.wait() == 0, "terminal input was not handed back cleanly"
            finally:
                try:
                    os.kill(child, signal.SIGTERM)
                except ProcessLookupError:
                    pass
    print("PASS: canceled partial password discarded; completed entry preserves the shell's next input")


def prompt_deadline(fixture):
    store = fixture.path / "nested" / "approved_hosts"
    args = fixture.args(store)
    args.remove("--password-stdin")
    before = authentication_count(fixture)
    with TerminalProcess(fixture, args) as process:
        process.wait_text("Trust this key and save it? [yes/NO]")
        time.sleep(11)
        assert process.poll() is None and authentication_count(fixture) == before
        process.send("yes\n")
        process.wait_text("SSH password:")
        time.sleep(11)
        assert process.poll() is None and authentication_count(fixture) == before
        process.send(fixture.password + "\n")
        process.ready()
        fixture.https()
        os.kill(process.pid, signal.SIGTERM)
        assert process.wait() == 0
        process.restored()
    assert store.stat().st_mode & 0o777 == 0o600
    assert store.parent.stat().st_mode & 0o777 == 0o700
    print("PASS: >10 seconds at each human prompt excluded from shared network deadline; private new trust storage")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="ubuntu:24.04")
    args = parser.parse_args()
    with Fixture(args.image) as fixture, observed_authentication(fixture):
        before = authentication_count(fixture)
        hidden_password(fixture)
        assert authentication_count(fixture) > before, "authentication observer did not record the successful login"
        enrollment(fixture)
        hashed_trust(fixture)
        patterned_trust(fixture)
        whitespace_trust(fixture)
        trust_matrix(fixture)
        refusal_and_persistence(fixture)
        noninteractive_password(fixture)
        terminal_cancellation(fixture)
        terminal_input_lifetime(fixture)
        prompt_deadline(fixture)
        before = authentication_count(fixture)
        certificate_only_server(fixture)
        assert authentication_count(fixture) == before


if __name__ == "__main__":
    main()
