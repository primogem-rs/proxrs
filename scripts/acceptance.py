#!/usr/bin/env python3
"""Build and run the macOS acceptance matrix with durable provenance."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
SUITES = ("smoke", "proxy", "trust", "http", "run", "recovery", "cleanup", "load")
IMAGES = ("ubuntu:24.04", "debian:12")


class EvidenceChanged(RuntimeError):
    pass


class Interrupted(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def source():
    """Identify dirty working files as well as the commit; never copy contents."""
    files = []
    names = set(git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0"))
    for name in sorted(names - {b""}):
        path = ROOT / os.fsdecode(name)
        item = {"path": os.fsdecode(name)}
        if path.is_symlink():
            item.update(kind="symlink", target=os.readlink(path))
        elif path.is_file():
            item.update(kind="file", mode=stat.S_IMODE(path.stat().st_mode), sha256=sha256(path))
        elif path.exists():
            raise RuntimeError(f"Unsupported source entry: {path}")
        else:
            item["kind"] = "missing"
        files.append(item)
    return {
        "revision": git("rev-parse", "HEAD").decode().strip(),
        "branch": git("rev-parse", "--abbrev-ref", "HEAD").decode().strip(),
        "dirty": bool(git("status", "--porcelain", "--untracked-files=all")),
        "status": git("status", "--porcelain", "--untracked-files=all").decode(),
        "files": files,
        "sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
    }


class Run:
    def __init__(self, output, suites, images):
        self.output = output
        self.signum = None
        self.active = None
        self.signal_delivered = False
        self.report = {
            "schema_version": 1, "started_at": now(), "status": "running",
            "host": {"platform": platform.platform(), "machine": platform.machine(),
                     "python": sys.version},
            "selection": {"suites": suites, "images": images},
            "binaries": {}, "steps": [],
        }
        profiles = ["debug"] if any(s != "smoke" for s in suites) else []
        if "load" in suites:
            profiles.append("release")
        for profile in profiles:
            argv = ["cargo", "build", "--locked", "--bin", "proxrs",
                    "--message-format=json-render-diagnostics"]
            if profile == "release":
                argv.append("--release")
            self.add_step("build-" + profile, "build", argv, profile=profile)
        for image in images:
            for suite in suites:
                argv = (["bash", "scripts/docker-ssh-smoke.sh", image] if suite == "smoke" else
                        [sys.executable, f"tests/docker/{suite}_cases.py", "--image", image])
                self.add_step(suite + "-" + image.replace(":", "-"),
                              "fixture" if suite == "smoke" else "proxrs", argv,
                              image=image, suite=suite,
                              profile=None if suite == "smoke" else "release" if suite == "load" else "debug")
        self.save()

    def verify_evidence(self):
        self.report["source_after"] = source()
        original = self.report["source"]
        after = self.report["source_after"]
        if original["revision"] != after["revision"] or original["sha256"] != after["sha256"]:
            raise EvidenceChanged("Source changed during acceptance; rerun against a stable working tree")
        for binary in self.report["binaries"].values():
            if sha256(self.output / binary["path"]) != binary["sha256"]:
                raise EvidenceChanged("Captured executable changed during acceptance")

    def add_step(self, name, category, argv, **extra):
        self.report["steps"].append(dict(name=name, category=category, command=argv,
                                         status="not_run", log=f"logs/{name}.log", **extra))

    def save(self):
        temporary = self.output / "manifest.json.tmp"
        temporary.write_text(json.dumps(self.report, indent=2) + "\n")
        temporary.replace(self.output / "manifest.json")

    def terminate(self, signum, _frame):
        if self.signum is None:
            self.signum = signum
            self.forward_signal()

    def forward_signal(self):
        if self.active and self.signum and not self.signal_delivered:
            process, category = self.active
            try:
                # Suites own their processes/containers and must unwind first.
                # Cargo has no fixture ownership: interrupt its compiler group.
                if category == "build":
                    os.killpg(process.pid, self.signum)
                else:
                    process.send_signal(self.signum)
                self.signal_delivered = True
            except ProcessLookupError:
                pass

    def step(self, step):
        if self.signum:
            raise Interrupted("Acceptance interrupted")
        environment = os.environ.copy()
        environment["PYTHONUNBUFFERED"] = "1"
        environment.pop("PYTHONOPTIMIZE", None)
        for profile, variable in (("debug", "PROXRS_TEST_BINARY"), ("release", "PROXRS_TEST_RELEASE_BINARY")):
            if profile in self.report["binaries"]:
                environment[variable] = str(self.output / self.report["binaries"][profile]["path"])
        if step["category"] == "proxrs":
            step["binary_sha256"] = self.report["binaries"][step["profile"]]["sha256"]
        if step["category"] != "build":
            step["fixture_log"] = f"fixtures/{step['name']}.jsonl"
            environment["PROXRS_TEST_FIXTURES"] = str(self.output / step["fixture_log"])
        if step.get("suite") == "load":
            step["results"] = f"results/{step['name']}.json"
            step["command"] += ["--output", str(self.output / step["results"])]
        step.update(status="running", started_at=now())
        self.save()
        print(f"RUN {step['name']} (log: {self.output / step['log']})", flush=True)
        started = time.monotonic()
        try:
            with (self.output / step["log"]).open("wb") as log:
                process = subprocess.Popen(step["command"], cwd=ROOT, env=environment,
                                           stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                self.active = (process, step["category"])
                # Also handles a signal arriving between spawn and registration.
                self.forward_signal()
                step["exit_code"] = process.wait()
            if self.signum:
                raise Interrupted("Acceptance interrupted; child cleanup finished")
            if step["exit_code"]:
                raise RuntimeError(f"{step['name']} exited {step['exit_code']}; see {step['log']}")
            if step["category"] == "build":
                self.capture_binary(step)
            step["status"] = "passed"
        except Exception as error:
            step.update(status="interrupted" if self.signum else "failed", error=str(error))
            raise
        finally:
            self.active = None
            step.update(ended_at=now(), duration_seconds=round(time.monotonic() - started, 3))
            step["log_sha256"] = sha256(self.output / step["log"])
            try:
                if "fixture_log" in step:
                    self.capture_fixtures(step)
            except Exception as error:
                step.update(status="failed", evidence_error=str(error))
                raise
            finally:
                self.save()
                print(f"{step['status'].upper()} {step['name']}", flush=True)

    def capture_fixtures(self, step):
        path = self.output / step["fixture_log"]
        events = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        step["fixtures"] = events
        step["fixture_log_sha256"] = sha256(path) if path.exists() else None
        built = {event["image_id"] for event in events if event["event"] == "built"}
        created = {event["container_id"] for event in events if event["event"] == "created"}
        removed = {event["container_id"] for event in events if event["event"] == "removed"}
        step["containers_not_recorded_removed"] = sorted(created - removed)
        if step["status"] == "passed":
            if not created or created != removed:
                raise RuntimeError("Fixture creation/cleanup evidence is incomplete")
            if any(event["image_id"] not in built for event in events if event["event"] == "created"):
                raise RuntimeError("Launched fixture image was not recorded as built")
        if "results" in step and (self.output / step["results"]).exists():
            step["measurements"] = json.loads((self.output / step["results"]).read_text())

    def tool_versions(self):
        versions = {}
        for tool, arguments in (("cargo", ["--version"]), ("rustc", ["-Vv"]),
                                ("docker", ["--version"]), ("ssh", ["-V"])):
            try:
                result = subprocess.run([tool, *arguments], stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, timeout=10)
                versions[tool] = {"exit_code": result.returncode, "output": result.stdout.strip()}
            except (OSError, subprocess.TimeoutExpired) as error:
                versions[tool] = {"error": str(error)}
        return versions

    def capture_binary(self, step):
        executable = None
        for line in (self.output / step["log"]).read_text().splitlines():
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if (message.get("reason") == "compiler-artifact" and
                    message.get("target", {}).get("name") == "proxrs" and message.get("executable")):
                executable = Path(message["executable"])
        if executable is None:
            raise RuntimeError("Cargo did not report the proxrs executable")
        destination = self.output / "bin" / step["profile"] / "proxrs"
        destination.parent.mkdir(parents=True)
        shutil.copyfile(executable, destination)
        destination.chmod(0o500)
        self.report["binaries"][step["profile"]] = {
            "path": str(destination.relative_to(self.output)), "built_path": str(executable),
            "profile": step["profile"], "sha256": sha256(destination),
            "size_bytes": destination.stat().st_size,
        }
        self.save()

    def execute(self):
        previous = {number: signal.signal(number, self.terminate)
                    for number in (signal.SIGINT, signal.SIGTERM)}
        try:
            self.report["source"] = source()
            self.report["tools"] = self.tool_versions()
            self.save()
            for step in self.report["steps"]:
                self.step(step)
                self.verify_evidence()
            self.report["status"] = "passed"
        except Interrupted as error:
            self.report.update(status="interrupted", error=str(error))
        except EvidenceChanged as error:
            self.report.update(status="invalid", error=str(error))
        except Exception as error:
            self.report.update(status="failed", error=str(error))
        finally:
            if self.signum:
                self.report.update(status="interrupted", signal=self.signum)
            if "source" in self.report:
                try:
                    self.verify_evidence()
                except EvidenceChanged as error:
                    self.report.update(status="invalid", error=str(error))
                except Exception as error:
                    self.report.update(status="invalid", error=f"Could not verify final evidence: {error}")
            self.report["ended_at"] = now()
            self.save()
            for number, handler in previous.items():
                signal.signal(number, handler)
        print(f"{self.report['status'].upper()}: {self.output / 'manifest.json'}", flush=True)
        return 128 + self.signum if self.signum else 0 if self.report["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suites", nargs="+", choices=SUITES, default=list(SUITES))
    parser.add_argument("--images", nargs="+", choices=IMAGES, default=list(IMAGES))
    parser.add_argument("--output", type=Path, help="New report directory (default: target/acceptance/<run>)")
    args = parser.parse_args()
    os.umask(0o077)
    output = (args.output or ROOT / "target/acceptance" /
              (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8])).resolve()
    try:
        relative = output.relative_to(ROOT)
    except ValueError:
        pass
    else:
        if subprocess.run(["git", "check-ignore", "-q", str(relative) + "/"], cwd=ROOT).returncode:
            parser.error("report directory inside the repository must be Git-ignored")
    output.mkdir(parents=True, exist_ok=False)
    for directory in ("logs", "fixtures", "results"):
        (output / directory).mkdir()
    return Run(output, list(dict.fromkeys(args.suites)), list(dict.fromkeys(args.images))).execute()


if __name__ == "__main__":
    sys.exit(main())
