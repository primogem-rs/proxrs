"""Test the runner's public CLI/report using real Git and Cargo projects.

Docker is an external boundary: a failing daemon is simulated without requiring
Docker for the baseline. Protocol acceptance remains in tests/docker.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]


class AcceptanceRunner(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="proxrs-runner-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "repo"
        self.root.mkdir()
        (self.root / "scripts").mkdir()
        runner = ROOT / "scripts/acceptance.py"
        if runner.exists():
            shutil.copyfile(runner, self.root / "scripts/acceptance.py")
        shutil.copyfile(ROOT / "scripts/docker-ssh-smoke.sh",
                        self.root / "scripts/docker-ssh-smoke.sh")
        shutil.copytree(ROOT / "tests/docker", self.root / "tests/docker")
        (self.root / "src").mkdir()
        (self.root / "src/main.rs").write_text("fn main() {}\n")
        (self.root / "Cargo.toml").write_text(
            '[package]\nname = "proxrs"\nversion = "0.0.0"\nedition = "2024"\n'
        )
        (self.root / ".gitignore").write_text("target/\n__pycache__/\n")
        self.command("cargo", "generate-lockfile", "--offline")
        self.command("git", "init", "-q")
        self.command("git", "add", ".")
        self.command("git", "-c", "user.name=Runner Test", "-c",
                     "user.email=test@example.invalid", "commit", "-qm", "fixture")
        self.output = Path(self.directory.name) / "report"
        self.argv = [sys.executable, str(self.root / "scripts/acceptance.py"),
                     "--suites", "proxy", "--images", "ubuntu:24.04",
                     "--output", str(self.output)]

    def command(self, *argv):
        return subprocess.run(argv, cwd=self.root, check=True, capture_output=True)

    def run_acceptance(self, **kwargs):
        return subprocess.run(self.argv, cwd=self.root, capture_output=True,
                              text=True, timeout=30, **kwargs)

    def report(self):
        return json.loads((self.output / "manifest.json").read_text())

    def external(self, name, body):
        directory = Path(self.directory.name) / "external"
        directory.mkdir(exist_ok=True)
        program = directory / name
        program.write_text(body)
        program.chmod(0o700)
        return {**os.environ, "PATH": str(directory) + os.pathsep + os.environ["PATH"]}

    def wait_for_output(self, process, path, text):
        deadline = time.monotonic() + 10
        while True:
            output = path.read_text()
            if text in output:
                return output
            self.assertIsNone(process.poll(), output)
            self.assertLess(time.monotonic(), deadline, output)
            time.sleep(.02)

    def test_progress_streams_complete_cases_before_exit_and_preserves_raw_log(self):
        release = Path(self.directory.name) / "release"
        output = Path(self.directory.name) / "stdout"
        first = b"PASS: " + b"x" * 70000 + b"\nordinary diagnostic\nPASS: first case\nPASS: streamed caf\xc3"
        final = b"\xa9\r\nPASS ubuntu:24.04: final case"
        env = self.external("docker", f'''#!{sys.executable}
import sys, time
from pathlib import Path
if sys.argv[1] != "build":
    raise SystemExit(0)
sys.stdout.buffer.write({first!r})
sys.stdout.buffer.flush()
while not Path({str(release)!r}).exists():
    time.sleep(.01)
sys.stdout.buffer.write({final!r})
sys.stdout.buffer.flush()
raise SystemExit(78)
''')
        self.argv[3] = "smoke"
        self.argv.append("--progress")
        with output.open("wb") as stdout:
            process = subprocess.Popen(self.argv, cwd=self.root, env=env,
                                       stdout=stdout, stderr=subprocess.STDOUT)
            try:
                live = self.wait_for_output(process, output, "] PASS: first case")
                self.assertIsNone(process.poll(), "progress arrived only after exit")
                self.assertRegex(live, r"\[smoke-ubuntu-24\.04 \+\d+s\] PASS: first case")
                self.assertNotIn("streamed", live, "partial line was reported as complete")
                self.assertNotIn("ordinary diagnostic", live)
                self.assertNotIn("xxxx", live, "oversized log line reached progress output")
                release.touch()
                process.wait(timeout=10)

                text = output.read_text()
                for result in ["PASS: first case", "PASS: streamed café", "PASS ubuntu:24.04: final case"]:
                    self.assertEqual(text.count(result), 1, text)
                self.assertEqual(process.returncode, 1)
                step = self.report()["steps"][0]
                self.assertEqual(step["status"], "failed")
                raw = (self.output / step["log"]).read_bytes()
                self.assertEqual(raw, first + final)
                self.assertEqual(step["log_sha256"], hashlib.sha256(raw).hexdigest())
            finally:
                release.touch()
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=10)

    def test_progress_heartbeats_report_the_last_case_after_thirty_quiet_seconds(self):
        allow_result = Path(self.directory.name) / "allow-result"
        release = Path(self.directory.name) / "release"
        output = Path(self.directory.name) / "stdout"
        env = self.external("docker", f'''#!{sys.executable}
import time
from pathlib import Path
import sys
if sys.argv[1] != "build":
    raise SystemExit(0)
while not Path({str(allow_result)!r}).exists():
    time.sleep(.01)
print("PASS: completed case", flush=True)
while not Path({str(release)!r}).exists():
    time.sleep(.01)
raise SystemExit(78)
''')
        self.argv[3] = "smoke"
        self.argv.append("--progress")
        # Accelerate the clock boundary, keeping subprocess waits on real time.
        # Exercise the actual CLI without a test-only production interval option.
        launcher = '''import runpy, subprocess, sys, time
clock = time.monotonic
time.monotonic = lambda: clock() * 120
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
'''
        argv = [sys.executable, "-c", launcher, *self.argv[1:]]
        with output.open("wb") as stdout:
            process = subprocess.Popen(argv, cwd=self.root, env=env,
                                       stdout=stdout, stderr=subprocess.STDOUT)
            try:
                self.wait_for_output(process, output, "RUNNING — no completed cases yet")
                allow_result.touch()
                self.wait_for_output(process, output, "] PASS: completed case")
                text = self.wait_for_output(process, output, "RUNNING — last result: completed case")
                self.assertIsNone(process.poll())
                events = re.findall(r"\[smoke-ubuntu-24\.04 \+(\d+)s\] (.+)", text)
                heartbeat = next(int(t) for t, line in events if line == "RUNNING — no completed cases yet")
                passed = next(int(t) for t, line in events if line == "PASS: completed case")
                later = next(int(t) for t, line in events if line == "RUNNING — last result: completed case")
                self.assertGreaterEqual(heartbeat, 30)
                self.assertGreaterEqual(later - passed, 30)
                self.assertEqual(text.count("] PASS: completed case"), 1)
            finally:
                allow_result.touch()
                release.touch()
                process.wait(timeout=10)

    def test_progress_encoding_failure_keeps_waiting_and_preserves_raw_log(self):
        cleaned = Path(self.directory.name) / "cleaned"
        env = self.external("docker", f'''#!{sys.executable}
import sys, time
from pathlib import Path
if sys.argv[1] != "build":
    raise SystemExit(0)
sys.stdout.buffer.write("PASS: café\\n".encode("utf-8"))
sys.stdout.buffer.flush()
time.sleep(.75)
Path({str(cleaned)!r}).touch()
print("child cleanup complete", flush=True)
raise SystemExit(78)
''')
        self.argv[3] = "smoke"
        self.argv.append("--progress")
        result = self.run_acceptance(env={**env, "PYTHONIOENCODING": "ascii:strict"})
        self.assertEqual(result.returncode, 1)
        step = self.report()["steps"][0]
        self.assertIn("exited 78", step["error"])
        self.assertTrue(cleaned.exists(), "progress failure abandoned the child")
        raw = (self.output / step["log"]).read_bytes()
        self.assertEqual(raw, "PASS: café\nchild cleanup complete\n".encode("utf-8"))
        self.assertEqual(step["log_sha256"], hashlib.sha256(raw).hexdigest())

    def test_failed_build_keeps_source_and_log_without_starting_suites(self):
        (self.root / "src/main.rs").write_text('compile_error!("intentional build failure");\n')
        result = self.run_acceptance()
        self.assertNotEqual(result.returncode, 0)
        report = self.report()
        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["source"]["dirty"])
        self.assertEqual(len(report["source"]["revision"]), 40)
        build = report["steps"][0]
        self.assertEqual(build["status"], "failed")
        self.assertIn("intentional build failure", (self.output / build["log"]).read_text())
        self.assertEqual(report["steps"][1]["status"], "not_run")
        self.assertEqual(report["binaries"], {})

    def test_fixture_failure_keeps_fresh_binary_and_skips_remaining_suites(self):
        external = Path(self.directory.name) / "external"
        external.mkdir()
        docker = external / "docker"
        docker.write_text("#!/bin/sh\necho 'test daemon unavailable' >&2\nexit 78\n")
        docker.chmod(0o700)
        stale = self.root / "target/debug/proxrs"
        stale.parent.mkdir(parents=True)
        stale.write_text("stale binary must never be tested")
        target = Path(self.directory.name) / "cargo-target"
        self.argv[3:4] = ["proxy", "trust"]
        result = self.run_acceptance(env={**os.environ, "PATH": str(external) + os.pathsep + os.environ["PATH"],
                                          "CARGO_TARGET_DIR": str(target)})
        self.assertNotEqual(result.returncode, 0)
        report = self.report()
        self.assertEqual(report["steps"][0]["status"], "passed")
        self.assertEqual(report["steps"][1]["status"], "failed")
        self.assertEqual(report["steps"][2]["status"], "not_run")
        binary = report["binaries"]["debug"]
        built = (target / "debug/proxrs").read_bytes()
        self.assertEqual((self.output / binary["path"]).read_bytes(), built)
        self.assertEqual(binary["sha256"], hashlib.sha256(built).hexdigest())
        self.assertEqual(binary["profile"], "debug")
        self.assertEqual(report["steps"][1]["binary_sha256"], binary["sha256"])
        self.assertEqual(report["steps"][1]["fixtures"], [])

    def test_source_change_during_build_invalidates_report_before_suites(self):
        cargo = shlex.quote(shutil.which("cargo"))
        env = self.external("cargo", f'#!/bin/sh\n{cargo} "$@"\nresult=$?\n'
                            'if [ "$1" = build ]; then echo "// changed" >> src/main.rs; fi\n'
                            'exit "$result"\n')
        self.external("docker", "#!/bin/sh\nexit 78\n")
        result = self.run_acceptance(env=env)
        self.assertNotEqual(result.returncode, 0)
        report = self.report()
        self.assertEqual(report["status"], "invalid")
        self.assertNotEqual(report["source"]["sha256"], report["source_after"]["sha256"])
        self.assertEqual(report["steps"][1]["status"], "not_run")

    def test_termination_waits_for_child_cleanup_and_preserves_report(self):
        self.assert_termination(progress=False)

    def test_progress_termination_waits_for_child_cleanup_and_preserves_report(self):
        self.assert_termination(progress=True)

    def assert_termination(self, progress):
        started = Path(self.directory.name) / "started"
        cleaned = Path(self.directory.name) / "cleaned"
        output = Path(self.directory.name) / "stdout"
        env = self.external("cargo", f'''#!{sys.executable}
import os, signal, sys, time
from pathlib import Path
if sys.argv[1] != "build":
    os.execv({shutil.which("cargo")!r}, [{shutil.which("cargo")!r}, *sys.argv[1:]])
def stop(number, frame):
    Path({str(cleaned)!r}).write_text("child cleanup complete")
    print("PASS: child cleanup complete", flush=True)
    raise SystemExit(128 + number)
signal.signal(signal.SIGTERM, stop)
Path({str(started)!r}).write_text(str(os.getpid()))
print("PASS: before cleanup", flush=True)
time.sleep(60)
''')
        if progress:
            self.argv.append("--progress")
        with output.open("wb") as stdout:
            process = subprocess.Popen(self.argv, cwd=self.root, env=env,
                                       stdout=stdout, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 10
                while not started.exists():
                    self.assertIsNone(process.poll())
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.02)
                if progress:
                    self.wait_for_output(process, output, "] PASS: before cleanup")
                process.terminate()
                process.wait(timeout=10)
                report = self.report()
                self.assertEqual(process.returncode, 143)
                self.assertEqual(report["status"], "interrupted")
                step = report["steps"][0]
                self.assertEqual(step["status"], "interrupted")
                self.assertEqual(report["steps"][1]["status"], "not_run")
                self.assertEqual(cleaned.read_text(), "child cleanup complete")
                raw = (self.output / step["log"]).read_bytes()
                self.assertEqual(raw, b"PASS: before cleanup\nPASS: child cleanup complete\n")
                self.assertEqual(step["log_sha256"], hashlib.sha256(raw).hexdigest())
                text = output.read_text()
                if progress:
                    self.assertEqual(text.count("] PASS: before cleanup"), 1)
                    self.assertEqual(text.count("] PASS: child cleanup complete"), 1)
                else:
                    self.assertNotIn("PASS:", text, "live progress must be opt-in")
            finally:
                if started.exists():
                    try:
                        os.kill(int(started.read_text()), signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
