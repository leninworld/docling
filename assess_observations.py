"""Unexecuted G0 decision rules; no candidate code and no project imports."""

import argparse
import json
from pathlib import Path

from observe_email import FIXTURES


def assess(reports):
    result = {"admit_candidate": False, "lanes": {}, "state": "INCOMPLETE"}
    for lane in ["L1", "L2", "L3"]:
        report = reports[lane]
        if report.get("lane") != lane:
            raise ValueError("Lane identity mismatch: " + lane)
        observed = report.get("state") == "OBSERVED"
        result["lanes"][lane] = {"observed": observed, "versions": report.get("versions"), "state": report.get("state")}
        if not observed:
            continue
        if set(report["messages"]) != set(FIXTURES):
            raise ValueError("Missing or extra fixture observations: " + lane)
        for name, expected in FIXTURES.items():
            message = report["messages"][name]
            if message.get("input_sha256") != expected:
                raise ValueError("Fixture identity mismatch: " + lane + "/" + name)
            if not isinstance(message.get("json_texts"), list):
                raise ValueError("Missing actual text-item JSON: " + lane)
            for field in ["markdown_default", "markdown_body_furniture", "text_default", "text_body_furniture"]:
                if not isinstance(message.get(field), str):
                    raise ValueError("Missing actual serialization: " + lane + "/" + field)
                if lane != "L1" and message[field].startswith("UNSUPPORTED_API:"):
                    raise ValueError("Required installed export API unavailable: " + lane + "/" + field)
        html = report["messages"]["html-only.eml"]
        plain = report["messages"]["plain.eml"]
        body = "Html: 1 < 2 & 3 > 2, a_b."
        body_observed = all(
            any(body in row.get("text", "") for row in message["json_texts"])
            for message in [html, plain]
        )
        footer_in_json = any("Footer text" in row.get("text", "") for row in html["json_texts"])
        footer_in_export = any(
            "Footer text" in html[field]
            for field in ["markdown_default", "markdown_body_furniture", "text_default", "text_body_furniture"]
            if not html[field].startswith("UNSUPPORTED_API:")
        )
        once = "Html: 1 &lt; 2 &amp; 3 &gt; 2" in html["markdown_default"]
        twice = "&amp;lt;" in html["markdown_default"]
        result["lanes"][lane].update(
            body_observed=body_observed,
            footer_in_json=footer_in_json,
            footer_in_export=footer_in_export,
            html_escaped_once=once and not twice,
            html_double_escaping_observed=twice,
        )
    main = result["lanes"]["L3"]
    latest = result["lanes"]["L2"]
    reported = result["lanes"]["L1"]
    if main.get("footer_in_json") or main.get("footer_in_export"):
        result["state"] = "STOP_MAIN_ALREADY_RETAINS_FOOTER"
    elif not reported.get("observed") and reports["L1"].get("state") != "REPORTED_EMAIL_DEPENDENCY_UNAVAILABLE":
        result["state"] = "HOLD_REPORTED_LANE_FAILED"
    elif not main.get("observed") or not latest.get("observed"):
        result["state"] = "HOLD_ENVIRONMENT_OR_MISSING_OBSERVATION"
    elif not main.get("body_observed") or not latest.get("body_observed"):
        result["state"] = "HOLD_CONVERSION_CONTENT_MISMATCH"
    elif not main.get("html_escaped_once"):
        result["state"] = "HOLD_MAIN_ESCAPING_DIFFERS_FROM_REVIEWED_DESIGN"
    else:
        result["state"] = "MAIN_FOOTER_LOSS_OBSERVED"
        result["admit_candidate"] = True
    # L1 can be honestly UNKNOWN if the exact reporter image lacks email
    # support. No replacement environment or reported-release pass is inferred.
    result["reported_environment_unknown"] = not result["lanes"]["L1"]["observed"]
    result["facet_one_credit"] = "Upstream merged #4828; not this contribution"
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reports", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    reports = {lane: json.loads((args.reports / (lane + ".json")).read_text()) for lane in ["L1", "L2", "L3"]}
    result = assess(reports)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    return 0 if result["admit_candidate"] else 22


if __name__ == "__main__":
    raise SystemExit(main())
