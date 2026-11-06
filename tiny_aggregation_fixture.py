"""UNRUN offline harness self-check. Synthetic reports are not Docling evidence."""

from copy import deepcopy
import json
from pathlib import Path
import sys

from assess_observations import assess
from observe_email import FIXTURES


def synthetic_reports():
    reports = {}
    for lane in ['L1', 'L2', 'L3']:
        messages = {}
        for name, digest in FIXTURES.items():
            messages[name] = {
                'input_sha256': digest,
                'markdown_default': 'Html: 1 &lt; 2 &amp; 3 &gt; 2, a_b.',
                'markdown_body_furniture': 'Html: 1 &lt; 2 &amp; 3 &gt; 2, a_b.',
                'text_default': 'Html: 1 < 2 & 3 > 2, a_b.',
                'text_body_furniture': 'Html: 1 < 2 & 3 > 2, a_b.',
                'json_texts': [{'text': 'Html: 1 < 2 & 3 > 2, a_b.', 'content_layer': 'body'}],
            }
        reports[lane] = {
            'lane': lane,
            'state': 'OBSERVED',
            'versions': {'identity': 'SYNTHETIC_HARNESS_FIXTURE_NOT_INSTALLED_VERSIONS'},
            'messages': messages,
            'candidate_code': False,
        }
    return reports


def main():
    checks = []
    original = synthetic_reports()
    result = assess(original)
    assert result['admit_candidate'] and result['state'] == 'MAIN_FOOTER_LOSS_OBSERVED'
    checks.append('synthetic positive admission predicate')

    unknown = deepcopy(original)
    unknown['L1'] = {'lane': 'L1', 'state': 'REPORTED_EMAIL_DEPENDENCY_UNAVAILABLE', 'candidate_code': False}
    result = assess(unknown)
    assert result['admit_candidate'] and result['reported_environment_unknown']
    checks.append('reported-image unknown is not a substitute reproduction')

    failed = deepcopy(original)
    failed['L1'] = {'lane': 'L1', 'state': 'ENVIRONMENT_OR_CONVERSION_ERROR', 'candidate_code': False}
    result = assess(failed)
    assert not result['admit_candidate'] and result['state'] == 'HOLD_REPORTED_LANE_FAILED'
    checks.append('unrelated reported-lane failure holds')

    for lane in ['L2', 'L3']:
        unknown = deepcopy(original)
        unknown[lane] = {'lane': lane, 'state': 'MISSING_OBSERVATION', 'candidate_code': False}
        result = assess(unknown)
        assert not result['admit_candidate'] and result['state'] == 'HOLD_ENVIRONMENT_OR_MISSING_OBSERVATION'
        checks.append(lane + ' unknown holds')

    for location in ['json', 'export']:
        retained = deepcopy(original)
        html = retained['L3']['messages']['html-only.eml']
        if location == 'json':
            html['json_texts'].append({'text': 'Footer text', 'content_layer': 'furniture'})
        else:
            html['markdown_body_furniture'] += '\nFooter text'
        result = assess(retained)
        assert not result['admit_candidate'] and result['state'] == 'STOP_MAIN_ALREADY_RETAINS_FOOTER'
        checks.append('main footer already in ' + location + ' stops')

    mismatch = deepcopy(original)
    mismatch['L3']['messages']['plain.eml']['json_texts'][0]['text'] = 'Different content'
    result = assess(mismatch)
    assert not result['admit_candidate'] and result['state'] == 'HOLD_CONVERSION_CONTENT_MISMATCH'
    checks.append('body mismatch holds')

    escaped = deepcopy(original)
    escaped['L3']['messages']['html-only.eml']['markdown_default'] = 'Html: 1 &amp;lt; 2'
    result = assess(escaped)
    assert not result['admit_candidate'] and result['state'] == 'HOLD_MAIN_ESCAPING_DIFFERS_FROM_REVIEWED_DESIGN'
    checks.append('different escaping holds')

    for problem in ['fixture hash', 'missing export', 'lane identity']:
        malformed = deepcopy(original)
        if problem == 'fixture hash':
            malformed['L3']['messages']['plain.eml']['input_sha256'] = '0' * 64
        elif problem == 'missing export':
            malformed['L3']['messages']['html-only.eml']['text_default'] = None
        else:
            malformed['L3']['lane'] = 'L2'
        try:
            assess(malformed)
        except ValueError:
            checks.append(problem + ' rejected')
        else:
            raise AssertionError('Malformed evidence was accepted: ' + problem)

    assert not any(name == 'docling' or name.startswith(('docling.', 'docling_core.')) for name in sys.modules)
    evidence = {'kind': 'SYNTHETIC_HARNESS_SELF_CHECK_ONLY', 'status': 'PASS', 'checks': checks,
                'project_imports': False, 'actual_docling_observations': False, 'candidate_code': False}
    destination = Path('/tmp/docling-g0-tiny/results')
    destination.mkdir(parents=True, exist_ok=False)
    encoded = (json.dumps(evidence, indent=2) + '\n').encode()
    (destination / 'self-check.json').write_bytes(encoded)
    assert json.loads((destination / 'self-check.json').read_bytes()) == evidence
    print(json.dumps(evidence))


if __name__ == '__main__':
    main()
