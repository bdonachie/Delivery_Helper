"""Reading config/project.json: defaults, and refusing mistakes by name."""

import json

import pytest

import project
from conftest import ROOT


def parse(document):
    return project.parse_project(document)


def test_only_the_project_key_is_required_and_the_rest_default():
    configured = parse({'jira': {'project': 'ABC'}})

    assert configured.jira_project == 'ABC'
    assert configured.tester_issue_type == 'Tester'
    assert configured.blocked_status == 'Blocked'
    assert configured.defects_epic == ''
    assert configured.end_date is None
    assert configured.attachments == ()


def test_the_shipped_example_is_valid():
    document = json.loads((ROOT / 'config' / 'project.example.json').read_text(encoding='utf-8'))

    assert parse(document).jira_project == 'UAT'


def test_comment_keys_are_ignored_at_every_level():
    configured = parse({'_comment': 'notes', 'jira': {'_note': 'x', 'project': 'ABC'}})

    assert configured.jira_project == 'ABC'


def test_a_missing_project_key_is_refused_by_name():
    with pytest.raises(project.ProjectConfigurationError, match='jira.project'):
        parse({'jira': {'testerIssueType': 'Tester'}})


def test_a_misspelt_key_is_refused_rather_than_silently_ignored():
    with pytest.raises(project.ProjectConfigurationError, match='jira.projet'):
        parse({'jira': {'project': 'ABC', 'projet': 'typo'}})


@pytest.mark.parametrize('section, key, value, message', [
    ('statuses', 'cancelled', "Won't Do", 'list'),
    ('statuses', 'cancelled', [], 'at least one'),
    ('schedule', 'endDate', '18/12/2026', 'YYYY-MM-DD'),
    ('schedule', 'testingWindowBusinessDays', -1, 'whole number'),
    ('schedule', 'testingWindowBusinessDays', True, 'whole number'),
    ('export', 'dateFormat', 'dd/mm/yyyy', 'strftime'),
    ('jira', 'testerIssueType', '   ', 'non-empty'),
])
def test_values_of_the_wrong_shape_are_refused_with_the_key_named(section, key, value, message):
    with pytest.raises(project.ProjectConfigurationError, match=message) as raised:
        parse({'jira': {'project': 'ABC'}, section: {key: value}})

    assert f'{section}.{key}' in str(raised.value)


def test_an_attachment_cannot_point_outside_the_attachments_folder():
    with pytest.raises(project.ProjectConfigurationError, match='not a path'):
        parse({'jira': {'project': 'ABC'},
               'emails': {'attachments': [{'file': '../../.env'}]}})


def test_an_attachment_without_a_name_is_sent_under_its_file_name():
    configured = parse({'jira': {'project': 'ABC'},
                        'emails': {'attachments': [{'file': 'guide.html'}]}})

    assert configured.attachments == (project.EmailAttachment('guide.html', 'guide.html'),)


def test_a_missing_file_says_how_to_create_one(tmp_path):
    with pytest.raises(project.ProjectConfigurationError, match='project.example.json'):
        project.load_project(tmp_path / 'project.json')


def test_invalid_json_is_reported_as_such(tmp_path):
    path = tmp_path / 'project.json'
    path.write_text('{"jira": ', encoding='utf-8')

    with pytest.raises(project.ProjectConfigurationError, match='not valid JSON'):
        project.load_project(path)
