"""UNRUN isolated entrypoint for trusted harness-only synthetic self-checks."""

from pathlib import Path
import runpy
import sys
import subprocess


root = Path("/harness")
if any((root / name).exists() for name in ["docling", "docling_core", "docling.py", "docling_core.py"]):
    raise RuntimeError("Harness-only tree contains a product-package shadow")
# -I disables implicit script/CWD/PYTHONPATH imports. Only this verified
# harness-only tree is explicitly allowed for its own helper modules here.
sys.path.insert(0, str(root))
for name in ["tiny_aggregation_fixture.py", "tiny_proxy_fixture.py", "tiny_lane_fixture.py"]:
    runpy.run_path(str(root / name), run_name="__main__")

assert sys.version_info[:2] == (3, 12), "HOLD_TOOLCHAIN_INCOMPATIBLE: Python"
version = subprocess.run(["uv", "--version"], check=True, capture_output=True, text=True, timeout=5).stdout
assert version.split()[:2] == ["uv", "0.11.8"], "HOLD_TOOLCHAIN_INCOMPATIBLE: uv"
# Supporting flag checks; actual L3 frozen export still decides lock compatibility.
for args, flags in [
    (["pip", "compile"], ["--generate-hashes", "--no-header", "--no-annotate", "--no-config", "--no-sources", "--exclude-newer", "--only-binary"]),
    (["export"], ["--project", "--package", "--frozen", "--no-default-groups", "--extra", "--no-emit-workspace"]),
    (["build"], ["--python", "--no-build-isolation", "--wheel", "--out-dir"]),
]:
    help_text = subprocess.run(["uv", *args, "--help"], check=True, capture_output=True, text=True, timeout=5).stdout
    assert all(flag in help_text for flag in flags), "HOLD_TOOLCHAIN_INCOMPATIBLE: flags"
print("Bootstrap Python3.12/uv0.11.8 flags verified; actual frozen-lock compatibility remains UNRUN")
