"""PRIVATE, UNRUN G0 consumer/main preparation; executes inside owned Docker only.

Network phase resolves and freezes before installing wheels. Offline phase
builds main from its immutable source and proves dependency consistency.
This is observation preparation, not a proposed product fix.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import tomllib
import urllib.request


WORK = Path("/work")
SOURCE = Path("/source")
ENV = WORK / "env"
BUILD = WORK / "build-env"


def remaining():
    seconds = float(os.environ["G0_STARTED_EPOCH"]) + 1200 - time.time()
    if seconds <= 0:
        raise RuntimeError("Original shared G0 deadline exhausted")
    return seconds


def run(name, argv, *, cwd=WORK):
    # The outer hosted controller kills the owned container at the original
    # deadline as well. This timeout does not create another per-command clock.
    with (WORK / (name + ".stdout")).open("xb") as out, (WORK / (name + ".stderr")).open("xb") as err:
        result = subprocess.run(argv, cwd=cwd, env=os.environ.copy(),
                                stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                timeout=remaining(), check=False)
    if result.returncode:
        raise RuntimeError(name + " failed: " + str(result.returncode))


def freeze_receipt(path):
    raw = path.read_bytes()
    if len(raw) > 2 * 1024 * 1024:
        raise RuntimeError("Requirements freeze exceeds reviewed evidence cap")
    text = raw.decode("utf-8")
    if any(token in text for token in ["http:", "https:", "git+", "file:", "-e ", " @ "]):
        raise RuntimeError("Freeze includes a nonregistry/local dependency")
    requirements = []
    for logical in text.replace("\\\n", " ").splitlines():
        line = logical.strip()
        if not line or line.startswith("#"):
            continue
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9_,.-]+\])?==[^\s;]+", line):
            raise RuntimeError("Freeze includes an unpinned requirement or option")
        if "--hash=sha256:" not in line:
            raise RuntimeError("Freeze includes a requirement without artifact SHA256")
        requirements.append(line.split(" --hash", 1)[0].strip())
    if not requirements:
        raise RuntimeError("Empty dependency freeze")
    result = {"filename": path.name, "sha256": hashlib.sha256(raw).hexdigest(),
              "bytes": len(raw), "requirements": requirements}
    (WORK / (path.name + ".receipt.json")).write_text(json.dumps(result, indent=2) + "\n")
    return result


def network_phase(lane, expected_version):
    remaining()
    if not os.environ.get("HTTPS_PROXY", "").startswith("http://"):
        raise RuntimeError("Missing reviewed internal-network acquisition proxy")
    run("uv-version", ["uv", "--version"])
    if (WORK / "uv-version.stdout").read_text().split()[:2] != ["uv", "0.11.8"]:
        raise RuntimeError("Unreviewed resolver version")
    run("create-env", ["uv", "venv", "--python", "/usr/local/bin/python3", "--no-python-downloads", str(ENV)])
    epoch = datetime.fromtimestamp(float(os.environ["G0_STARTED_EPOCH"]), timezone.utc).isoformat()
    requirements = WORK / "requirements.txt"
    if lane == "L2":
        # Independent real PyPI consumer. No main uv.lock or constraint file is
        # mounted in this lane; a changed latest version stops this exact packet.
        with urllib.request.urlopen("https://pypi.org/pypi/docling-slim/json", timeout=min(20, remaining())) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise RuntimeError("Latest release metadata exceeds cap")
        metadata = json.loads(raw)
        if metadata["info"]["version"] != expected_version:
            raise RuntimeError("Latest published release changed; exact packet is stale")
        (WORK / "latest-release.json").write_text(json.dumps({
            "version": metadata["info"]["version"], "raw_response_bytes": len(raw),
            "raw_response_sha256": hashlib.sha256(raw).hexdigest()}) + "\n")
        incoming = WORK / "requirements.in"
        incoming.write_text("docling-slim[format-email]==" + expected_version + "\n")
        run("freeze-consumer", ["uv", "pip", "compile", str(incoming), "--generate-hashes",
                              "--no-header", "--no-annotate", "--no-config", "--no-sources",
                              "--python", str(ENV / "bin/python"), "--exclude-newer", epoch,
                              "--only-binary", ":all:", "--output-file", str(requirements)])
    else:
        try:
            run("export-main-lock", ["uv", "export", "--project", str(SOURCE),
                                 "--package", "docling-slim", "--frozen", "--no-default-groups",
                                 "--extra", "format-email", "--no-emit-workspace", "--no-header",
                                 "--no-annotate", "--output-file", str(requirements)])
        except RuntimeError as error:
            raise RuntimeError("HOLD_TOOLCHAIN_INCOMPATIBLE: frozen main lock export failed") from error
        # Main's actual unversioned hatchling build requirement is resolved and
        # hash-frozen separately. It never constrains the independent L2 lane.
        build_input = WORK / "build.in"
        build_requires = tomllib.loads((SOURCE / "pyproject.toml").read_text())["build-system"]["requires"]
        if build_requires != ["hatchling"]:
            raise RuntimeError("Source build requirements changed")
        build_input.write_text("\n".join(build_requires) + "\n")
        run("freeze-build-backend", ["uv", "pip", "compile", str(build_input), "--generate-hashes",
                                    "--no-header", "--no-annotate", "--no-config", "--no-sources",
                                    "--python", str(ENV / "bin/python"), "--exclude-newer", epoch,
                                    "--only-binary", ":all:", "--output-file", str(WORK / "build.txt")])
        freeze_receipt(WORK / "build.txt")
        run("create-build-env", ["uv", "venv", "--python", "/usr/local/bin/python3", "--no-python-downloads", str(BUILD)])
        run("install-build-backend", ["uv", "pip", "sync", str(WORK / "build.txt"), "--require-hashes",
                                      "--only-binary", ":all:", "--python", str(BUILD / "bin/python")])
    freeze_receipt(requirements)
    run("install-frozen-wheels", ["uv", "pip", "sync", str(requirements), "--require-hashes",
                                  "--only-binary", ":all:", "--python", str(ENV / "bin/python")])


def copy_source(source, target):
    for link in source.rglob("*"):
        if link.is_symlink():
            resolved = link.resolve(strict=True)
            if not resolved.is_relative_to(source):
                raise RuntimeError("Source symlink leaves immutable input")
    shutil.copytree(source, target, symlinks=True, ignore=shutil.ignore_patterns(".git"))


def offline_phase(lane):
    remaining()
    if os.environ.get("UV_OFFLINE") != "1" or os.environ.get("HTTPS_PROXY"):
        raise RuntimeError("Offline preparation retains a network/proxy setting")
    if lane == "L3":
        # Build in an owned copy so hatchling cannot alter immutable input.
        target = WORK / "source-build"
        copy_source(SOURCE, target)
        run("build-main-wheel", ["uv", "build", "--python", str(BUILD / "bin/python"),
                                 "--no-build-isolation", "--wheel", "--out-dir", str(WORK / "dist/docling-slim"), str(target)])
        wheels = list((WORK / "dist/docling-slim").glob("docling_slim-*.whl"))
        if len(wheels) != 1:
            raise RuntimeError("Expected exactly one branch-source slim wheel")
        wheel = wheels[0]
        (WORK / "main-wheel.json").write_text(json.dumps({"filename": wheel.name,
            "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(), "bytes": wheel.stat().st_size}) + "\n")
        run("install-main-wheel", ["uv", "pip", "install", "--no-deps", "--no-index",
                                   "--python", str(ENV / "bin/python"), str(wheel)])
    run("dependency-consistency", ["uv", "pip", "check", "--python", str(ENV / "bin/python")])
    run("installed-distributions", ["uv", "pip", "list", "--format", "json", "--python", str(ENV / "bin/python")])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lane", choices=["L2", "L3"], required=True)
    parser.add_argument("--phase", choices=["network", "offline"], required=True)
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()
    if args.expected_version != "2.148.0":
        raise RuntimeError("Version differs from reviewed G0 contract")
    if args.phase == "network":
        network_phase(args.lane, args.expected_version)
    else:
        offline_phase(args.lane)


if __name__ == "__main__":
    main()
