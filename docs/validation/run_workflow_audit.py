"""Run isolated audit checks; never load the operational DB or contact ERP.

Run from the repository root:
  python docs/validation/run_workflow_audit.py baseline
  python docs/validation/run_workflow_audit.py focused
"""
import contextlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import socket
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.test_settings'
import django
django.setup()
from django.conf import settings
from django.test.runner import DiscoverRunner

assert settings.DATABASES['default']['NAME'] == ':memory:'
assert settings.ERP_ENABLED is False
phase = sys.argv[1] if len(sys.argv) > 1 else 'baseline'
assert phase in ('baseline', 'focused', 'fixes')
records = []
summary = {}
blocked_network = []


class Result(unittest.TextTestResult):
    def addSuccess(self, test):
        super().addSuccess(test)
        records.append({'test': test.id(), 'status': 'pass'})

    def addFailure(self, test, err):
        super().addFailure(test, err)
        records.append({'test': test.id(), 'status': 'failure', 'detail': self._exc_info_to_string(err, test)})

    def addError(self, test, err):
        super().addError(test, err)
        records.append({'test': test.id(), 'status': 'error', 'detail': self._exc_info_to_string(err, test)})

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        records.append({'test': test.id(), 'status': 'skipped', 'detail': reason})

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err:
            records.append({'test': subtest.id(), 'status': 'subtest_failure', 'detail': self._exc_info_to_string(err, test)})


class TestRunner(unittest.TextTestRunner):
    resultclass = Result

    def run(self, suite):
        result = super().run(suite)
        summary.update(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped))
        return result


class Runner(DiscoverRunner):
    test_runner = TestRunner


def deny_network(*args, **kwargs):
    blocked_network.append('Blocked outbound socket connection')
    raise AssertionError('Audit forbids outbound network connections')


labels = ['docs.validation.workflow_cases'] if phase == 'focused' else ['orders', 'qms', 'material']
stream = io.StringIO()
with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream), patch.object(socket.socket, 'connect', deny_network), patch.object(socket.socket, 'connect_ex', deny_network):
    result_code = Runner(verbosity=2, interactive=False).run_tests(labels)

output = Path(__file__).parent
(output / f'{phase}-test-log.txt').write_text(stream.getvalue(), encoding='utf-8')
(output / f'{phase}-results.json').write_text(json.dumps({
    'generated_at_utc': datetime.now(timezone.utc).isoformat(), 'phase': phase,
    'database': 'isolated SQLite in-memory', 'erp': 'disabled / mocked',
    'historical_migrations': 'disabled by config.test_settings; not migration verification',
    'blocked_network_attempts': len(blocked_network), 'summary': summary,
    'tests': records,
}, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'phase': phase, **summary, 'blocked_network_attempts': len(blocked_network)}))
for record in records:
    if record['status'] not in ('pass', 'skipped'):
        print(record['test'], record['status'])
print('Evidence saved in docs/validation. Operational DB and ERP were not used.')
sys.exit(bool(result_code))
