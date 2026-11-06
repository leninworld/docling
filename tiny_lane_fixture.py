"""UNRUN Docker-only synthetic input/freezing/copy guard tests; no Docling."""

import json
from pathlib import Path
import tempfile

import lane_install
from g0_runner import Runner


def main():
    checks = []
    with tempfile.TemporaryDirectory(prefix="g0-lane-") as directory:
        root = Path(directory)
        lane_install.WORK = root
        source = root / "pristine"
        (source / "nested").mkdir(parents=True)
        (source / "nested/file.txt").write_text("Synthetic source only\n")
        (source / ".git").mkdir()
        (source / ".git/private-marker").write_text("SYNTHETIC_NOT_A_CREDENTIAL")
        target = root / "cases/copy"
        lane_install.copy_source(source, target)
        assert (target / "nested/file.txt").read_bytes() == (source / "nested/file.txt").read_bytes()
        assert not (target / ".git").exists()
        checks.append("actual nested source copy excludes Git metadata")
        try:
            lane_install.copy_source(source, target)
        except FileExistsError:
            checks.append("existing destination cannot be overwritten")
        else:
            raise AssertionError("Source destination collision accepted")
        (source / "escape").symlink_to(root)
        try:
            lane_install.copy_source(source, root / "unsafe-copy")
        except RuntimeError:
            checks.append("source symlink escaping immutable input rejected")
        else:
            raise AssertionError("Escaping source link accepted")

        requirements = root / "requirements.txt"
        requirements.write_text("package==1.0 \\\n    --hash=sha256:" + "a" * 64 + "\n")
        receipt = lane_install.freeze_receipt(requirements)
        assert receipt["requirements"] == ["package==1.0"]
        checks.append("actual hash-frozen requirements and receipt")
        for name, content in [
            ("missing-hash", "package==1.0\n"),
            ("unpinned", "package>=1\n"),
            ("url", "package @ https://example.com/file.whl\n"),
            ("empty", "# no requirements\n"),
        ]:
            path = root / (name + ".txt")
            path.write_text(content)
            try:
                lane_install.freeze_receipt(path)
            except RuntimeError:
                checks.append(name + " freeze rejected")
            else:
                raise AssertionError("Unsafe freeze accepted: " + name)
    # No hosted-job variables are forwarded into the tiny container.
    try:
        Runner("L2")
    except RuntimeError as error:
        assert "restricted" in str(error)
        checks.append("actual runner refuses execution outside exact hosted job")
    else:
        raise AssertionError("Runner admitted synthetic/nonhosted execution")
    print(json.dumps({"kind": "SYNTHETIC_HARNESS_SELF_CHECK_ONLY", "status": "PASS",
                      "checks": checks, "external_network_requests": 0,
                      "project_imports": False, "actual_docling_observations": False}))


if __name__ == "__main__":
    main()
