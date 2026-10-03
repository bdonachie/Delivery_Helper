"""Small builders for the objects the tests pass around, and a stand-in Jira."""

import requests

import uat_jira

JIRA_SITE = 'https://example.atlassian.net'


def make_item(key='UAT-1', status='To Do', status_category='new', tester_name='Jane Smith',
              **overrides) -> uat_jira.TesterItem:
    values = dict(
        key=key, url=f'{JIRA_SITE}/browse/{key}', summary=f'Test {key}-Platform-JaneSmith',
        test_name=f'Test {key}', platform='Platform', tester_name=tester_name,
        instrument='', action='', status=status, status_category=status_category,
        assignee='', start_date='', due_date='', created='', updated='', description='',
        parent_key='UAT-10', parent_summary='UAT - Trading',
    )
    values.update(overrides)
    return uat_jira.TesterItem(**values)


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=''):
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'{self.status_code}')


class FakeJira:
    """Records every call and answers the handful of endpoints the app uses."""

    jira_site = JIRA_SITE

    def __init__(self, transitions=None, created_key='UAT-999'):
        self.calls = []
        self.transitions = transitions or [{'id': '21', 'to': {'name': 'In Progress'}}]
        self.created_key = created_key

    def _record(self, method, url, **keywords):
        self.calls.append((method, url.replace(self.jira_site, ''), keywords))

    def get(self, url, params=None, timeout=None):
        self._record('GET', url, params=params)
        if url.endswith('/transitions'):
            return FakeResponse({'transitions': self.transitions})
        return FakeResponse({'issues': [], 'isLast': True})

    def post(self, url, json=None, timeout=None):
        self._record('POST', url, json=json)
        if url.endswith('/rest/api/3/issue'):
            return FakeResponse({'key': self.created_key}, 201)
        if url.endswith('/search/jql') or url.endswith('/bulkfetch'):
            return FakeResponse({'issues': [], 'isLast': True})
        return FakeResponse({}, 204)

    def put(self, url, json=None, timeout=None):
        self._record('PUT', url, json=json)
        return FakeResponse({}, 204)

    def paths(self, method):
        return [path for call_method, path, _ in self.calls if call_method == method]


class UnusableJira:
    """Fails the test if anything tries to reach Jira."""

    jira_site = JIRA_SITE

    def __getattr__(self, name):
        raise AssertionError(f'Jira was called ({name}) when nothing should have been fetched')
