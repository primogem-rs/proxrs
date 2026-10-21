"""Test the runner's public CLI/report using real Git and Cargo projects.

Docker is an external boundary: a failing daemon is simulated without requiring
Docker for the baseline. Protocol acceptance remains in tests/docker.
"""
import hashlib
import json
import os
from pathlib import Path
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
        started = Path(self.directory.name) / "started"
        cleaned = Path(self.directory.name) / "cleaned"
        env = self.external("cargo", f'''#!{sys.executable}
import os, signal, sys, time
from pathlib import Path
if sys.argv[1] != "build":
    os.execv({shutil.which("cargo")!r}, [{shutil.which("cargo")!r}, *sys.argv[1:]])
def stop(number, frame):
    Path({str(cleaned)!r}).write_text("child cleanup complete")
    raise SystemExit(128 + number)
signal.signal(signal.SIGTERM, stop)
Path({str(started)!r}).write_text(str(os.getpid()))
time.sleep(60)
''')
        process = subprocess.Popen(self.argv, cwd=self.root, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not started.exists():
                self.assertIsNone(process.poll())
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.02)
            process.terminate()
            process.communicate(timeout=10)
            report = self.report()
            self.assertEqual(process.returncode, 143)
            self.assertEqual(report["status"], "interrupted")
            self.assertEqual(report["steps"][0]["status"], "interrupted")
            self.assertEqual(report["steps"][1]["status"], "not_run")
            self.assertEqual(cleaned.read_text(), "child cleanup complete")
        finally:
            if started.exists():
                try:
                    os.kill(int(started.read_text()), signal.SIGTERM)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
