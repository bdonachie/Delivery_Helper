"""The web app, through Flask's test client, against a fake Jira."""

import pytest

import app as web
import uat_jira
from factories import FakeJira, make_item


@pytest.fixture
def client(monkeypatch):
    web.issue_cache.update({'items': [], 'loaded_at': None, 'fetched_monotonic': None})
    web.application.config['TESTING'] = True
    with web.application.test_client() as test_client:
        yield test_client


@pytest.fixture
def jira(monkeypatch):
    fake = FakeJira()
    monkeypatch.setattr(web, 'jira_session', lambda: fake)
    return fake


@pytest.fixture
def items(monkeypatch):
    current = [make_item(key='UAT-1', status='To Do'), make_item(key='UAT-2', status='Blocked')]
    monkeypatch.setattr(uat_jira, 'fetch_tester_items', lambda session, progress=True: current)
    return current


def sign_in(client, user_id='jane', access_code=None):
    return client.post('/api/session', json={'userId': user_id, 'accessCode': access_code})


def test_the_health_check_answers_without_signing_in(client):
    response = client.get('/healthz')

    assert response.status_code == 200 and response.get_json() == {'status': 'ok'}


def test_signed_out_api_calls_get_401_and_pages_redirect(client):
    api = client.get('/api/issues')
    page = client.get('/')

    assert api.status_code == 401 and api.get_json()['signedOut'] is True
    assert page.status_code == 302 and '/login' in page.headers['Location']


def test_signing_in_names_the_person_and_never_returns_a_token(client):
    assert sign_in(client).status_code == 200

    response = client.get('/api/session')
    assert response.get_json()['user']['name'] == 'Jane Smith'
    body = response.get_data(as_text=True)
    assert 'token-for-jane' not in body and 'token-for-sam' not in body
    assert 'apiToken' not in body


def test_an_access_code_is_required_when_configured(client):
    assert sign_in(client, 'sam').status_code == 403
    assert sign_in(client, 'sam', 'wrong').status_code == 403
    assert sign_in(client, 'sam', 'let-me-in').status_code == 200


def test_unknown_people_and_people_without_a_token_cannot_sign_in(client):
    assert sign_in(client, 'nobody-here').status_code == 404
    assert sign_in(client, 'no-token-person').status_code == 400


def test_the_page_carries_the_application_name(client):
    sign_in(client)

    page = client.get('/').get_data(as_text=True)

    assert '<title>Delivery Helper</title>' in page
    assert client.get('/login').status_code == 200


def test_issues_are_served_from_jira_through_the_signed_in_session(client, jira, items):
    sign_in(client)

    payload = client.get('/api/issues').get_json()

    assert [issue['key'] for issue in payload['issues']] == ['UAT-1', 'UAT-2']
    assert payload['excludedStatuses'] == ['Blocked']


def test_a_bulk_transition_skips_items_no_longer_in_the_expected_status(client, jira, items):
    sign_in(client)

    response = client.post('/api/bulk', json={'issueKeys': ['UAT-1', 'UAT-2'],
                                              'statusFrom': 'To Do', 'statusTo': 'In Progress'})

    results = {result['key']: result for result in response.get_json()['results']}
    assert results['UAT-1']['ok'] is True
    assert results['UAT-2']['ok'] is False and 'skipped' in results['UAT-2']['detail']
    assert jira.paths('POST') == ['/rest/api/3/issue/UAT-1/transitions']


def test_a_new_defect_is_created_in_the_configured_project_and_epic(client, jira):
    sign_in(client)

    response = client.post('/api/bugs/create', json={'summary': 'Gateway down'})

    assert response.get_json()['key'] == 'UAT-999'
    fields = jira.calls[0][2]['json']['fields']
    assert fields['project'] == {'key': 'UAT'}
    assert fields['parent'] == {'key': 'UAT-100'}


def test_compliance_is_empty_rather_than_broken_when_no_epic_is_configured(client, jira):
    sign_in(client)

    payload = client.get('/api/compliance').get_json()

    assert payload['items'] == [] and payload['epicKey'] == ''
    assert jira.calls == []


def test_downloads_are_limited_to_generated_files(client):
    sign_in(client)
    exports = web.settings.EXPORT_DIRECTORY
    (exports / 'report.csv').write_text('a,b\n', encoding='utf-8')
    (exports.parent.parent / 'outside.txt').write_text('secret', encoding='utf-8')

    served = client.get('/download/report.csv')
    escaped = client.get('/download/..%2F..%2Foutside.txt')

    assert served.status_code == 200 and 'attachment' in served.headers['Content-Disposition']
    assert escaped.status_code == 404


def test_the_export_hands_back_a_download_link_and_not_a_server_path(client, jira, items):
    sign_in(client)

    payload = client.post('/api/export', json={}).get_json()

    assert payload['downloadUrl'].startswith('/download/tests_by_tester_')
    assert 'filePath' not in payload
    assert payload['blockedCount'] == 1
