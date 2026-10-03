"""Point the app at a throwaway config and data directory before anything imports it.

settings resolves its directories at import, and project.json is read when the Jira layer
is first imported, so this has to happen at the top of conftest - before any test module
imports the app. Nothing here, or in any test, talks to a real Jira.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))

WORKSPACE = Path(tempfile.mkdtemp(prefix='delivery-helper-tests-'))
CONFIG_DIRECTORY = WORKSPACE / 'config'
DATA_DIRECTORY = WORKSPACE / 'data'
(CONFIG_DIRECTORY / 'attachments').mkdir(parents=True)

os.environ.update({
    'UAT_CONFIG_DIRECTORY': str(CONFIG_DIRECTORY),
    'UAT_DATA_DIRECTORY': str(DATA_DIRECTORY),
    'UAT_ENVIRONMENT_FILE': str(WORKSPACE / 'absent.env'),
    'UAT_SECRET_KEY': 'test-secret',
    'JIRA_SITE': 'https://example.atlassian.net',
    'JIRA_API_TOKEN_JANE': 'token-for-jane',
    'UAT_ACCESS_CODE_SAM': 'let-me-in',
})

TEST_PROJECT = {
    'jira': {'project': 'UAT', 'defaultPriority': 'Medium'},
    'epics': {'defects': 'UAT-100', 'readiness': 'UAT-101', 'devDefects': 'UAT-102',
              'compliance': ''},
    'schedule': {'testingWindowBusinessDays': 2, 'endDate': '2026-12-18'},
    'emails': {'defectsRaisedBy': 'the test leads', 'questionsChannel': '#uat-help',
               'attachments': [{'file': 'guide.html', 'name': 'Testing guide.html'}]},
    'export': {'excludedTesters': ['Excluded Person'], 'dateFormat': '%d/%m/%Y'},
}
(CONFIG_DIRECTORY / 'project.json').write_text(json.dumps(TEST_PROJECT), encoding='utf-8')
(CONFIG_DIRECTORY / 'attachments' / 'guide.html').write_text('<p>guide</p>', encoding='utf-8')
(CONFIG_DIRECTORY / 'users.json').write_text(json.dumps({'users': [
    {'id': 'jane', 'name': 'Jane Smith', 'email': 'jane.smith@example.com',
     'apiTokenEnvironmentVariable': 'JIRA_API_TOKEN_JANE'},
    {'id': 'sam', 'name': 'Sam Lee', 'email': 'sam.lee@example.com', 'apiToken': 'token-for-sam',
     'accessCodeEnvironmentVariable': 'UAT_ACCESS_CODE_SAM'},
    {'name': 'No Token Person', 'email': 'nobody@example.com'},
]}), encoding='utf-8')
(CONFIG_DIRECTORY / 'testers.json').write_text(json.dumps({'testers': [
    {'name': 'Jane Smith', 'email': 'jane.smith@example.com', 'accountId': 'acc-jane', 'confirmed': True},
    {'name': 'Sam Lee', 'email': 'sam.lee@example.com', 'accountId': 'acc-sam', 'confirmed': False},
    {'name': 'Excluded Person', 'email': 'excluded@example.com', 'accountId': 'acc-x', 'confirmed': True},
]}), encoding='utf-8')


@pytest.fixture
def history_directory(tmp_path, monkeypatch):
    """A fresh history folder, so snapshot tests cannot see each other's files."""
    import email_builder
    monkeypatch.setattr(email_builder, 'SUMMARY_HISTORY_DIRECTORY', tmp_path)
    return tmp_path
