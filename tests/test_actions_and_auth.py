"""Jira write helpers, and resolving who can sign in."""

from datetime import date

import pytest

import auth
import jira_actions


def test_business_days_skip_the_weekend():
    friday = date(2026, 10, 2)

    assert jira_actions.add_business_days(friday, 2) == date(2026, 10, 6)


def test_the_testing_window_comes_from_the_project():
    assert jira_actions.TESTING_WINDOW_BUSINESS_DAYS == 2
    assert jira_actions.START_DATE_FIELD == 'customfield_10015'


def test_a_comment_becomes_one_paragraph_per_line_with_blanks_dropped():
    document = jira_actions.build_comment_document('First line\n\nSecond line')

    assert document['type'] == 'doc'
    assert [paragraph['content'][0]['text'] for paragraph in document['content']] == ['First line', 'Second line']


def test_a_secret_in_the_environment_wins_over_one_written_inline(monkeypatch):
    monkeypatch.setenv('SOME_TOKEN', 'from-environment')
    entry = {'apiToken': 'inline', 'apiTokenEnvironmentVariable': 'SOME_TOKEN'}

    assert auth._resolve_secret(entry, 'apiToken', 'apiTokenEnvironmentVariable') == 'from-environment'


def test_an_inline_secret_is_used_when_the_environment_has_none(monkeypatch):
    monkeypatch.delenv('SOME_TOKEN', raising=False)
    entry = {'apiToken': 'inline', 'apiTokenEnvironmentVariable': 'SOME_TOKEN'}

    assert auth._resolve_secret(entry, 'apiToken', 'apiTokenEnvironmentVariable') == 'inline'


def test_a_user_without_an_id_is_identified_by_their_name():
    users = {user.name: user for user in auth.load_users()}

    assert users['No Token Person'].identifier == 'no-token-person'
    assert not users['No Token Person'].has_credentials
    assert users['Sam Lee'].requires_access_code


def test_a_missing_user_list_is_reported_rather_than_treated_as_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(auth.settings, 'USERS_FILE', tmp_path / 'users.json')

    with pytest.raises(auth.UserDirectoryError, match='users.example.json'):
        auth.load_users()
