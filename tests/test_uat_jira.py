"""The Jira data layer: reading titles, rich text and links into tester items."""

import pytest

import uat_jira
from factories import FakeJira, FakeResponse, UnusableJira, make_item

TESTERS = {'janesmith': 'Jane Smith', 'samlee': 'Sam Lee'}


def test_the_query_uses_the_configured_project_and_issue_type():
    assert uat_jira.TESTER_ISSUE_JQL == 'project = "UAT" AND issuetype = "Tester" ORDER BY key'


@pytest.mark.parametrize('summary, test_name, platform, tester', [
    ('Bond Options+Manual Capture-Order Gateway-JaneSmith',
     'Bond Options+Manual Capture', 'Order Gateway', 'Jane Smith'),
    # Read right to left, so hyphens inside the test name survive.
    ('Step-Out non-Ack by T+1-Infrastructure-SamLee',
     'Step-Out non-Ack by T+1', 'Infrastructure', 'Sam Lee'),
])
def test_titles_are_split_from_the_right(summary, test_name, platform, tester):
    parts = uat_jira.parse_tester_summary(summary, TESTERS)

    assert (parts['test_name'], parts['platform'], parts['tester_name']) == (test_name, platform, tester)


def test_an_unknown_trailing_name_keeps_the_whole_title_rather_than_misattributing_it():
    parts = uat_jira.parse_tester_summary('Settle a bond-Platform-SomebodyElse', TESTERS)

    assert parts == {'test_name': 'Settle a bond-Platform-SomebodyElse', 'platform': '',
                     'tester_name': '', 'instrument': '', 'action': ''}


def test_rich_text_is_flattened_with_mentions_breaks_and_image_markers():
    document = {'type': 'doc', 'content': [
        {'type': 'paragraph', 'content': [
            {'type': 'mention', 'attrs': {'text': '@Jane Smith'}},
            {'type': 'text', 'text': ' please check'},
            {'type': 'hardBreak'},
            {'type': 'text', 'text': 'thanks'},
        ]},
        {'type': 'mediaSingle', 'content': [{'type': 'media', 'attrs': {'alt': 'shot.png'}}]},
    ]}

    text = uat_jira.render_rich_text(document)

    assert text == '@Jane Smith please check\nthanks\n' + uat_jira.MEDIA_MARKER + 'shot.png' + uat_jira.MEDIA_MARKER


def test_an_issue_becomes_a_tester_item_with_its_source_and_defect_links():
    raw_issue = {'key': 'UAT-7', 'fields': {
        'summary': 'Cancel an order-Order Gateway-JaneSmith',
        'status': {'name': 'Blocked', 'statusCategory': {'key': 'indeterminate'}},
        'customfield_10015': '2026-10-01', 'duedate': '2026-10-03',
        'parent': {'key': 'UAT-10', 'fields': {'summary': 'UAT - Orders'}},
        'issuelinks': [
            {'outwardIssue': {'key': 'UAT-3', 'fields': {'issuetype': {'name': 'Task'}}}},
            {'inwardIssue': {'key': 'UAT-50', 'fields': {'issuetype': {'name': 'Bug'}}}},
            {'inwardIssue': {'key': 'UAT-51', 'fields': {'issuetype': {'name': 'Action Item'}}}},
        ],
        'comment': {'comments': [{'author': {'displayName': 'Jane Smith'}, 'created': '2026-10-02',
                                  'body': {'type': 'doc', 'content': [
                                      {'type': 'paragraph', 'content': [{'type': 'text', 'text': 'stuck'}]}]}}]},
    }}

    item = uat_jira.build_tester_item('https://example.atlassian.net', raw_issue, TESTERS)

    assert item.tester_name == 'Jane Smith'
    assert item.start_date == '2026-10-01'
    assert item.source_key == 'UAT-3'
    assert item.linked_bug_keys == ['UAT-50', 'UAT-51']      # both defect types count as coverage
    assert item.linked_bug_only_keys == ['UAT-50']           # but only Bugs are "bugs"
    assert item.latest_comment.body == 'stuck'
    assert item.url == 'https://example.atlassian.net/browse/UAT-7'


def test_blocked_work_needs_an_open_defect_and_a_stale_link_is_surfaced():
    uncovered = make_item(status='Blocked', linked_bug_keys=['UAT-50'], covering_bug_keys=[])
    covered = make_item(status='Blocked', linked_bug_keys=['UAT-50'], covering_bug_keys=['UAT-50'])

    assert uncovered.needs_bug and uncovered.has_stale_bug_link
    assert not covered.needs_bug and not covered.has_stale_bug_link


@pytest.mark.parametrize('status, category, eligible', [
    ('To Do', 'new', True),
    ('In Progress', 'indeterminate', True),
    ('Blocked', 'indeterminate', False),
    ('Testing Failed', 'indeterminate', False),
    ('Done', 'done', False),
    ("Won't Do", 'done', False),
])
def test_only_work_still_to_do_goes_in_a_testers_email(status, category, eligible):
    assert make_item(status=status, status_category=category).is_email_eligible is eligible


def test_a_date_on_the_item_wins_over_the_linked_schedule():
    assert make_item(start_date='2026-10-05', source_start_date='2026-10-01').effective_start_date == '2026-10-05'
    assert make_item(start_date='', source_start_date='2026-10-01').effective_start_date == '2026-10-01'


def test_a_subtask_source_supplies_action_and_instrument_but_a_task_does_not():
    subtask_item = make_item(key='UAT-1', source_key='UAT-3')
    task_item = make_item(key='UAT-2', source_key='UAT-4')

    class SourceJira(FakeJira):
        def post(self, url, json=None, timeout=None):
            return FakeResponse({'issues': [
                {'key': 'UAT-3', 'fields': {'summary': 'Cancel', 'issuetype': {'name': 'Sub-task', 'subtask': True},
                                            'parent': {'fields': {'summary': 'ETD Options'}},
                                            'status': {'name': 'To Do'}}},
                {'key': 'UAT-4', 'fields': {'summary': 'Standalone case', 'issuetype': {'name': 'Task', 'subtask': False},
                                            'parent': {'fields': {'summary': 'UAT - Epic'}},
                                            'status': {'name': 'To Do'}}},
            ]})

    uat_jira.attach_source_details(SourceJira(), [subtask_item, task_item])

    # Recognised by Jira's own flag, whatever the sub-task type happens to be called.
    assert (subtask_item.action, subtask_item.instrument) == ('Cancel', 'ETD Options')
    assert (task_item.action, task_item.instrument) == ('', '')


def test_an_unconfigured_epic_returns_nothing_without_asking_jira():
    assert uat_jira.COMPLIANCE_EPIC_KEY == ''
    assert uat_jira.fetch_compliance_items(UnusableJira()) == []


def test_defects_are_searched_for_in_the_configured_project_and_epics():
    jira = FakeJira()

    uat_jira.fetch_bugs(jira)

    _method, _path, keywords = jira.calls[0]
    assert keywords['params']['jql'].startswith('project = "UAT" AND issuetype in ("Action Item", "Bug")')
    assert 'parent in (UAT-100, UAT-101, UAT-102)' in keywords['params']['jql']
