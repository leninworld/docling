"""PRIVATE, UNRUN hosted G0 runner; requires a separately approved exact manifest.

No project code executes on the host. This script refuses macOS, self-hosted
runners, other repositories/branches, stale clocks and unapproved packet bytes.
No automatic retry, container/image removal, prune or substitute environment.
"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import time

from g0_https_proxy import Budget, Proxy


ROOT = Path(__file__).resolve().parent
REPORTED = "quay.io/docling-project/docling-serve-cpu@sha256:07accda3be8d1ca4c9968be0fb90356a4f5aa390beb1dfd1471459eaaf588bc5"
SOURCE_SHA = "1e26b84c5f5690128ddcd65a20a5d7ffb9e80dcd"
REQUIRED = {"g0_runner.py", "g0_https_proxy.py", "lane_install.py", "observe_email.py",
            "assess_observations.py", "tiny_aggregation_fixture.py", "tiny_proxy_fixture.py", "tiny_lane_fixture.py", "tiny_entry.py", ".gitattributes",
            "fixtures/html-only.eml", "fixtures/plain.eml"}


class Runner:
    def __init__(self, lane):
        if (platform.system() != "Linux" or platform.machine() != "x86_64"
                or os.environ.get("GITHUB_ACTIONS") != "true"
                or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
                or os.environ.get("GITHUB_REPOSITORY") != "leninworld/docling"
                or os.environ.get("GITHUB_REF") != "refs/heads/validate-docling5219-observation"):
            raise RuntimeError("Execution is restricted to the exact agent-controlled hosted diagnostic route")
        self.epoch = float(os.environ["G0_STARTED_EPOCH"])
        self.end = self.epoch + 1200
        self.monotonic_end = time.monotonic() + max(0, self.end - time.time())
        manifest_bytes = (ROOT / "manifest.json").read_bytes()
        approval = json.loads((ROOT / "execution-approval.json").read_text())
        if approval.get("execution_approved") is not True or approval.get("manifest_sha256") != hashlib.sha256(manifest_bytes).hexdigest():
            raise RuntimeError("Missing exact execution approval")
        self.manifest = json.loads(manifest_bytes)
        if (self.manifest.get("candidate_code") is not False or self.manifest.get("wall_seconds") != 1200
                or self.manifest.get("sequence") != ["L1", "L2", "L3"]):
            raise RuntimeError("Manifest scope differs from approved G0")
        files = self.manifest["files"]
        if not REQUIRED.issubset(files):
            raise RuntimeError("Manifest omits required controller/fixture identities")
        for name, expected in files.items():
            file = (ROOT / name).resolve()
            if not file.is_relative_to(ROOT) or not file.is_file():
                raise RuntimeError("Manifest file scope mismatch")
            if hashlib.sha256(file.read_bytes()).hexdigest() != expected:
                raise RuntimeError("Packet bytes changed after execution review: " + name)
        self.consumer_image = self.manifest["consumer_image"]
        if not re.fullmatch(r"ghcr\.io/astral-sh/uv@sha256:[0-9a-f]{64}", self.consumer_image):
            raise RuntimeError("Consumer image is not the reviewed immutable official uv image")
        if not 0 < self.manifest["consumer_compressed_bytes"] <= 500 * 1024 ** 2:
            raise RuntimeError("Consumer/bootstrap image exceeds reviewed compressed-size bound")
        if self.manifest["reported_image"] != REPORTED or self.manifest["source_commit"] != SOURCE_SHA:
            raise RuntimeError("Reported/source identity changed")
        self.lane = lane
        identity = os.environ["GITHUB_RUN_ID"] + "-" + os.environ["GITHUB_RUN_ATTEMPT"] + "-" + lane.lower()
        if not re.fullmatch(r"[0-9]+-[0-9]+-l[123]", identity):
            raise RuntimeError("Invalid hosted run identity")
        self.label = identity
        self.name = "docling5219-g0-" + identity
        self.output = ROOT / ("evidence-" + lane)
        self.output.mkdir(exist_ok=False)
        self.uid_gid = str(os.getuid()) + ":" + str(os.getgid())
        self.disk_roots = [self.output]
        self.commands = 0
        self.containers = []
        self.proxy = None
        self.budget = None
        self.env = {k: os.environ[k] for k in ["PATH", "HOME"] if k in os.environ}
        docker_config = self.output / "docker-config"
        docker_config.mkdir()
        self.env["DOCKER_CONFIG"] = str(docker_config)
        self.remaining()

    def remaining(self):
        remaining = min(self.end - time.time(), self.monotonic_end - time.monotonic())
        if remaining <= 0:
            raise RuntimeError("Original shared G0 deadline exhausted")
        return remaining

    def command(self, label, argv, cap_seconds=480):
        self.commands += 1
        number = str(self.commands).zfill(3)
        stdout = self.output / (number + "-" + label + ".stdout")
        stderr = self.output / (number + "-" + label + ".stderr")
        end = time.monotonic() + min(cap_seconds, self.remaining())
        started = time.monotonic()
        with stdout.open("xb") as out, stderr.open("xb") as err:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                       env=self.env, cwd=ROOT, start_new_session=True)
            try:
                while process.poll() is None:
                    if self.budget and self.budget.stopped.is_set():
                        raise RuntimeError("Acquisition proxy stopped: " + str(self.budget.failure))
                    self.remaining()
                    if time.monotonic() >= end:
                        raise RuntimeError("Owned command exhausted its original remaining time")
                    if stdout.stat().st_size + stderr.stat().st_size > 16 * 1024 * 1024:
                        raise RuntimeError("Owned command log cap exceeded")
                    if any(shutil.disk_usage(root).free < 1024 ** 3 for root in self.disk_roots):
                        raise RuntimeError("Hosted workspace reserve below 1GiB")
                    time.sleep(0.2)
            except BaseException:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
                raise
        receipt = {"label": label, "argv": argv, "seconds": time.monotonic() - started,
                   "returncode": process.returncode,
                   "stdout_sha256": hashlib.sha256(stdout.read_bytes()).hexdigest(),
                   "stderr_sha256": hashlib.sha256(stderr.read_bytes()).hexdigest()}
        (self.output / (number + "-receipt.json")).write_text(json.dumps(receipt, indent=2) + "\n")
        self.remaining()
        if process.returncode:
            raise RuntimeError(label + " failed: " + str(process.returncode))
        return stdout.read_bytes()

    def resources(self):
        memory = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            memory[key] = int(value.strip().split()[0]) * 1024
        info = json.loads(self.command("docker-storage", ["docker", "info", "--format", "{{json .}}"], 15))
        docker_root = Path(info["DockerRootDir"])
        if not docker_root.is_dir():
            raise RuntimeError("Docker storage filesystem cannot be measured")
        self.disk_roots.append(docker_root)
        disk = shutil.disk_usage(self.output)
        facts = {"cpu_count": os.cpu_count(), "memory_available": memory["MemAvailable"],
                 "disk_free": disk.free, "docker_root": str(docker_root),
                 "docker_driver": info["Driver"], "docker_disk_free": shutil.disk_usage(docker_root).free, "minimum_disk_free": (8 if self.lane == "L1" else 4) * 1024 ** 3}
        (self.output / "resource-preflight.json").write_text(json.dumps(facts, indent=2) + "\n")
        if facts["cpu_count"] < 4 or facts["memory_available"] < 8 * 1024 ** 3 or min(disk.free, facts["docker_disk_free"]) < facts["minimum_disk_free"]:
            raise RuntimeError("Measured hosted resource preflight failed; no cleanup or retry")

    def pull(self, image, label):
        self.command(label + "-pull", ["docker", "pull", "--platform", "linux/amd64", image], 300)
        result = json.loads(self.command(label + "-identity", ["docker", "image", "inspect", image]))[0]
        if result["Os"] != "linux" or result["Architecture"] != "amd64" or not (image in (result.get("RepoDigests") or [])
                or result["Id"] == self.manifest["image_config_digests"][label]):
            raise RuntimeError("Pulled image identity/platform mismatch")

    def tiny(self):
        # The public diagnostic tree must be an orphan containing only reviewed
        # harness files and its exact approval receipts, never product source.
        header = self.command("harness-root", ["git", "cat-file", "-p", "HEAD"]).decode().split("\n\n", 1)[0]
        if any(line.startswith("parent ") for line in header.splitlines()):
            raise RuntimeError("Diagnostic branch is not an orphan root")
        tree = set(self.command("harness-tree", ["git", "ls-tree", "-r", "--name-only", "HEAD"]).decode().splitlines())
        expected = set(self.manifest["files"]) | {"manifest.json", "execution-approval.json"}
        if tree != expected:
            raise RuntimeError("Diagnostic tree differs from reviewed harness-only allowlist")
        name = self.name + "-tiny"
        self.containers.append(name)
        self.command("tiny-harness", ["docker", "run", "--name", name,
            "--label", "org.docling5219.g0=" + self.label, "--pull", "never",
            "--network", "none", "--user", self.uid_gid, "-e", "HOME=/tmp", "--read-only", "--memory", "256m", "--cpus", "1", "--pids-limit", "128",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=32m", "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-v", str(ROOT) + ":/harness:ro", "--workdir", "/harness",
            "--entrypoint", "/bin/sh", self.consumer_image,
            "-c", "python3 -I /harness/tiny_entry.py"], 30)

    def setup(self):
        network = "none"
        proxy_url = None
        if self.lane != "L1":
            network = self.name + "-acquire"
            self.command("create-internal-network", ["docker", "network", "create", "--internal",
                "--label", "org.docling5219.g0=" + self.label, network])
            info = json.loads(self.command("inspect-internal-network", ["docker", "network", "inspect", network]))[0]
            if not info["Internal"]:
                raise RuntimeError("Acquisition network is not isolated")
            gateway = info["IPAM"]["Config"][0]["Gateway"]
            self.budget = Budget(deadline_epoch=self.end, max_bytes=512 * 1024 ** 2,
                                 max_connections=128, allowed_hosts=["pypi.org", "files.pythonhosted.org"])
            self.proxy = Proxy((gateway, 8769), self.budget)
            self.proxy.start()
            proxy_url = "http://" + gateway + ":8769"
        (self.output / "home").mkdir()
        image = REPORTED if self.lane == "L1" else self.consumer_image
        argv = ["docker", "create", "--name", self.name, "--pull", "never",
                "--label", "org.docling5219.g0=" + self.label, "--network", network,
                "--memory", "8g", "--memory-swap", "8g", "--cpus", "4", "--pids-limit", "2048",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--user", self.uid_gid, "--workdir", "/work",
                "--entrypoint", "/bin/sh", "-v", str(ROOT) + ":/harness:ro",
                "-v", str(self.output) + ":/work:rw"]
        variables = {"HOME": "/work/home", "G0_STARTED_EPOCH": str(self.epoch), "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                     "HF_DATASETS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1",
                     "UV_HTTP_RETRIES": "0", "UV_PYTHON_DOWNLOADS": "never", "UV_NO_CONFIG": "1",
                     "UV_DEFAULT_INDEX": "https://pypi.org/simple", "UV_CACHE_DIR": "/work/uv-cache",
                     "UV_CONCURRENT_DOWNLOADS": "4", "PYTHONDONTWRITEBYTECODE": "1", "NO_PROXY": ""}
        if proxy_url:
            variables.update(HTTPS_PROXY=proxy_url, HTTP_PROXY=proxy_url)
        for key, value in variables.items():
            argv += ["-e", key + "=" + value]
        if self.lane == "L3":
            source = ROOT / "upstream-source"
            if self.command("source-head", ["git", "-C", str(source), "rev-parse", "HEAD"]).decode().strip() != SOURCE_SHA:
                raise RuntimeError("Immutable main source identity mismatch")
            if self.command("source-clean", ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all"]):
                raise RuntimeError("Main input source is dirty")
            argv += ["-v", str(source) + ":/source:ro"]
        argv += [image, "-c", "sleep 1200"]
        self.containers.append(self.name)
        self.command("create-owned-container", argv)
        self.command("start-owned-container", ["docker", "start", self.name])
        if self.lane != "L1":
            probe = ("import socket; s=socket.create_connection((" + repr(gateway) + ",8769),5); "
                     "s.settimeout(5); s.sendall(b'CONNECT g0-reachability.invalid:443 HTTP/1.1\\r\\n\\r\\n'); "
                     "assert s.recv(128).startswith(b'HTTP/1.1 403 G0-Reachability'); s.close()")
            try:
                self.command("internal-proxy-reachability", ["docker", "exec", self.name,
                    "python3", "-I", "-c", probe], 10)
            except Exception as error:
                raise RuntimeError("HOLD_INTERNAL_PROXY_UNREACHABLE") from error
            self.command("freeze-install", ["docker", "exec", self.name, "python3", "-I", "/harness/lane_install.py",
                "--lane", self.lane, "--phase", "network", "--expected-version", "2.148.0"])
            self.command("disconnect-acquisition", ["docker", "network", "disconnect", network, self.name])
            networks = json.loads(self.command("verify-offline", ["docker", "inspect", self.name]))[0]["NetworkSettings"]["Networks"]
            if networks:
                raise RuntimeError("Observation container still has network connectivity")
            self.proxy.shutdown()
            self.proxy.server_close()
            self.proxy = None
            (self.output / "network-receipt.json").write_text(json.dumps(self.budget.snapshot(), indent=2) + "\n")
            if self.budget.failure:
                raise RuntimeError("Acquisition proxy failed")
            self.command("offline-build-check", ["docker", "exec", "-e", "HTTPS_PROXY=", "-e", "HTTP_PROXY=",
                "-e", "UV_OFFLINE=1", self.name, "python3", "-I", "/harness/lane_install.py",
                "--lane", self.lane, "--phase", "offline", "--expected-version", "2.148.0"])

    def observe(self):
        python = "python" if self.lane == "L1" else "/work/env/bin/python"
        argv = ["docker", "exec", "-e", "HTTPS_PROXY=", "-e", "HTTP_PROXY=", "-e", "UV_OFFLINE=1",
                self.name, python, "-I", "/harness/observe_email.py", "--lane", self.lane,
                "--fixture-dir", "/harness/fixtures", "--output", "/work/observation.json"]
        if self.lane == "L1":
            argv += ["--image-digest", REPORTED.split("@", 1)[1]]
        self.command("observe-real-converter", argv)
        report = (self.output / "observation.json").read_bytes()
        if len(report) > 196608:
            raise RuntimeError("Observation receipt cap exceeded")
        value = json.loads(report)
        permitted = ["OBSERVED"] + (["REPORTED_EMAIL_DEPENDENCY_UNAVAILABLE"] if self.lane == "L1" else [])
        if value["lane"] != self.lane or value["candidate_code"] is not False or value["state"] not in permitted:
            raise RuntimeError("Observation was not a permitted complete result")
        return report

    def stop_owned(self):
        # Stop exactly this run's labelled containers. Do not remove containers,
        # networks, images or volumes, and never invoke a global Docker command.
        for name in self.containers:
            inspect = subprocess.run(["docker", "inspect", name], env=self.env, capture_output=True, timeout=10)
            if inspect.returncode:
                continue
            item = json.loads(inspect.stdout)[0]
            if item["Config"]["Labels"].get("org.docling5219.g0") != self.label:
                raise RuntimeError("Container ownership mismatch; refusing stop")
            if item["State"]["Running"]:
                subprocess.run(["docker", "kill", name], env=self.env, capture_output=True, timeout=10, check=True)
        if self.proxy is not None:
            self.proxy.shutdown()
            self.proxy.server_close()
        if self.budget is not None:
            (self.output / "network-receipt.json").write_text(json.dumps(self.budget.snapshot(), indent=2) + "\n")

    def evidence(self):
        total = 0
        for path in sorted(self.output.iterdir()):
            if not path.is_file() or path.suffix not in (".json", ".stdout", ".stderr", ".txt", ".in"):
                continue
            raw = path.read_bytes()
            total += len(raw)
            if total > 8 * 1024 ** 2:
                raise RuntimeError("Synthetic public evidence log cap exceeded")
            print("G0_EVIDENCE " + json.dumps({"lane": self.lane, "name": path.name,
                "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                "base64": base64.b64encode(raw).decode()}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lane", choices=["L1", "L2", "L3"], required=True)
    parser.add_argument("--emit-github-output", action="store_true")
    args = parser.parse_args()
    runner = Runner(args.lane)
    try:
        runner.resources()
        runner.pull(runner.consumer_image, "consumer")
        runner.tiny()
        if args.lane == "L1":
            runner.pull(REPORTED, "reported")
        runner.setup()
        report = runner.observe()
        runner.remaining()
        if args.emit_github_output:
            with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                output.write("observation=" + base64.b64encode(report).decode() + "\n")
                output.write("sha256=" + hashlib.sha256(report).hexdigest() + "\n")
    except Exception as error:
        (runner.output / "runner-error.json").write_text(json.dumps({"state": "FAILED_NO_RETRY",
            "error": type(error).__name__ + ": " + str(error), "lane": args.lane,
            "candidate_code": False}) + "\n")
        raise
    finally:
        runner.stop_owned()
        runner.evidence()


if __name__ == "__main__":
    main()
