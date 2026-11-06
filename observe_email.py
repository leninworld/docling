"""Unexecuted G0 observation, using the public converter and synthetic mail only."""

import argparse
from datetime import datetime, timezone
import hashlib
from importlib import metadata
from importlib.util import find_spec
import io
import inspect
import json
import os
from pathlib import Path
import platform
import sys
import time
import traceback


FIXTURES = {
    "html-only.eml": "ace593df78186e7ec6abc2ec21f4af0c826b7a3821e50886c3065cda877c9ab6",
    "plain.eml": "c568877bb84626a825b5cb05c78b090e7bfe85c318f337b4fc2c168d8c862746",
}


def installed_version(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def layered_export(method, layers, lane, errors, name):
    if lane == "L1":
        try:
            inspect.signature(method).bind(included_content_layers=layers)
        except TypeError:
            errors[name] = "UNSUPPORTED_API: included_content_layers is absent from this installed signature"
            return errors[name]
    # A TypeError from the serializer body is a real lane failure, not evidence
    # of an unsupported old-version API.
    return method(included_content_layers=layers)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lane", choices=["L1", "L2", "L3"], required=True)
    parser.add_argument("--fixture-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image-digest")
    args = parser.parse_args()
    began = time.monotonic()
    report = {
        "lane": args.lane,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "state": "INCOMPLETE",
        "python": platform.python_version(),
        "versions": {
            name: installed_version(name)
            for name in ["docling-slim", "docling", "docling-core", "mail-parser", "docling-serve"]
        },
        "image_digest": args.image_digest,
        "messages": {},
        "candidate_code": False,
    }
    try:
        if sys.version_info[:2] != (3, 12):
            raise RuntimeError("G0 requires actual Python 3.12 in each lane")
        if os.environ.get("HF_HUB_OFFLINE") != "1":
            raise RuntimeError("Missing mandatory offline model guard")
        if args.lane == "L1":
            if args.image_digest != "sha256:07accda3be8d1ca4c9968be0fb90356a4f5aa390beb1dfd1471459eaaf588bc5":
                raise RuntimeError("Reporter image identity mismatch")
            for name, expected in [
                ("docling-slim", "2.138.0"),
                ("docling-core", "2.101.1"),
                ("docling-serve", "1.37.0"),
            ]:
                if report["versions"][name] != expected:
                    raise RuntimeError("Reported environment mismatch: " + name)
            if find_spec("mailparser") is None:
                report["state"] = "REPORTED_EMAIL_DEPENDENCY_UNAVAILABLE"
                report["missing_dependency"] = "mailparser"
                return 0
        elif report["versions"]["docling-slim"] != "2.148.0":
            raise RuntimeError("Latest/source artifact identity mismatch")

        # These imports and conversions are deliberately inside the recorded
        # observation: dependency failures must never become a product finding.
        from docling.datamodel.base_models import ConversionStatus, DocumentStream
        from docling.document_converter import DocumentConverter
        from docling_core.types.doc import ContentLayer

        layers = {ContentLayer.BODY, ContentLayer.FURNITURE}
        for name, expected in FIXTURES.items():
            raw = (args.fixture_dir / name).read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected:
                raise RuntimeError("Synthetic fixture identity mismatch: " + name)
            result = DocumentConverter().convert(
                DocumentStream(name=name, stream=io.BytesIO(raw))
            )
            if result.status != ConversionStatus.SUCCESS:
                raise RuntimeError("Conversion did not report SUCCESS: " + str(result.status))
            document = result.document
            export_errors = {}
            serialized = document.export_to_dict()
            texts = serialized.get("texts")
            if not isinstance(texts, list):
                raise RuntimeError("Unexpected document JSON text schema")
            # Retain the actual serializations. The separate aggregator decides
            # whether the current defect was observed; this helper makes no fix.
            report["messages"][name] = {
                "input_sha256": expected,
                "conversion_status": str(result.status),
                "markdown_default": document.export_to_markdown(),
                "markdown_body_furniture": layered_export(document.export_to_markdown,
                    layers, args.lane, export_errors, "markdown_body_furniture"),
                "text_default": document.export_to_text(),
                "text_body_furniture": layered_export(document.export_to_text,
                    layers, args.lane, export_errors, "text_body_furniture"),
                "export_api_gaps": export_errors,
                "json_texts": texts,
                "json_document": serialized,
            }
        report["state"] = "OBSERVED"
    except Exception as error:
        report["state"] = "ENVIRONMENT_OR_CONVERSION_ERROR"
        report["error"] = type(error).__name__ + ": " + str(error)
        report["traceback"] = traceback.format_exc()
    finally:
        report["seconds"] = time.monotonic() - began
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"lane": args.lane, "state": report["state"], "seconds": report["seconds"]}))
    return 0 if report["state"] == "OBSERVED" else 21


if __name__ == "__main__":
    raise SystemExit(main())
