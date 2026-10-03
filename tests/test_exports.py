"""The exports: status buckets, changelog dates, test case parsing and the per-tester CSV."""

import csv

import pytest

import export_by_tester
import export_uat_status
import uat_jira
from factories import FakeJira, make_item


@pytest.mark.parametrize('status, category, bucket', [
    ('Done', 'done', 'Completed'),
    ('Blocked', 'indeterminate', 'Blocked'),
    ('Testing Failed', 'indeterminate', 'Failed'),
    ('In Progress', 'indeterminate', 'Outstanding'),
    # Cancelled statuses sit in Jira's 'done' category. Counting them as Completed once
    # reported 764 complete when only 91 tests had passed.
    ("Won't Do", 'done', 'Cancelled'),
    ('Dupe', 'done', 'Cancelled'),
])
def test_statuses_fall_into_the_right_reporting_bucket(status, category, bucket):
    assert export_uat_status.status_bucket(make_item(status=status, status_category=category)) == bucket


def test_changelog_entries_are_sorted_before_being_read():
    histories = [
        {'created': '2026-10-05T09:00', 'items': [{'field': 'status', 'fromString': 'In Progress', 'toString': 'Done'}]},
        {'created': '2026-10-01T09:00', 'items': [{'field': 'status', 'fromString': 'To Do', 'toString': 'In Progress'}]},
        {'created': '2026-10-02T09:00', 'items': [{'field': 'assignee', 'fromString': 'a', 'toString': 'b'}]},
    ]

    assert export_by_tester.read_status_changes(histories) == [
        ('2026-10-01T09:00', 'to do', 'in progress'),
        ('2026-10-05T09:00', 'in progress', 'done'),
    ]


def test_the_actual_start_is_the_first_departure_from_to_do_and_completion_the_last_done():
    changes = [
        ('2026-10-01T09:00', 'to do', 'in progress'),
        ('2026-10-02T09:00', 'in progress', 'to do'),      # pushed back and restarted
        ('2026-10-03T09:00', 'to do', 'in progress'),
        ('2026-10-04T09:00', 'in progress', 'done'),
        ('2026-10-06T09:00', 'done', 'testing failed'),    # reopened
        ('2026-10-08T09:00', 'testing failed', 'done'),
    ]

    assert export_by_tester.find_actual_start_date(changes) == '2026-10-01'
    assert export_by_tester.find_actual_completion_date(changes) == '2026-10-08'


def test_test_case_sections_are_read_from_their_labels():
    detail = ('UAT Ref: UAT-12\nTest Case: Cancel an order\nPre-Conditions: Gateway up\n'
              'Functional Steps:\nOpen the order\nPress cancel\nExpected Result: Order cancelled')

    sections = export_by_tester.parse_test_case_sections(detail)

    assert sections['Pre-Conditions'] == 'Gateway up'
    assert sections['Expected Result'] == 'Order cancelled'
    assert export_by_tester.format_functional_steps(sections['Functional Steps']) == '1. Open the order\n2. Press cancel'


def test_steps_that_already_carry_numbers_keep_them():
    assert export_by_tester.format_functional_steps('1. Open\n2. Close') == '1. Open\n2. Close'


@pytest.mark.parametrize('labels, test_type', [
    (['area:orders', 'type:edge'], 'Edge Case'),
    (['type:smoke-test'], 'Smoke Test'),
    (['area:orders'], ''),
])
def test_the_test_type_comes_from_the_type_label(labels, test_type):
    assert export_by_tester.test_type_from_labels(labels) == test_type


@pytest.mark.parametrize('status, result', [
    ('Done', 'Pass'), ('Testing Failed', 'Fail'), ('Blocked', 'Blocked'), ('Retest', 'In Progress'),
    ('To Do', 'Pending'), ('Dupe', 'Not Applicable'), ('Under Review', ''),
])
def test_statuses_translate_to_a_result(status, result):
    assert export_by_tester.result_for(status) == result


def test_dates_are_written_in_the_configured_format_and_odd_values_are_kept():
    assert export_by_tester.format_date('2026-10-01T09:30:00.000+1000') == '01/10/2026'
    assert export_by_tester.format_date('next week') == 'next week'
    assert export_by_tester.format_date('') == ''


def test_test_ids_sort_naturally():
    assert sorted(['UAT-10', 'UAT-2', 'UAT-1'], key=export_uat_status.natural_sort_key) == ['UAT-1', 'UAT-2', 'UAT-10']


def test_image_markers_never_reach_a_csv_cell():
    marked = f'see {uat_jira.MEDIA_MARKER}shot.png{uat_jira.MEDIA_MARKER}  now'

    assert export_uat_status.tidy(marked) == 'see [image: shot.png] now'


def test_one_row_per_tester_and_excluded_testers_are_left_out():
    shared = dict(source_key='UAT-3', test_case_detail='UAT Ref: UAT-12\nTest Case: Cancel an order')
    items = [
        make_item(key='UAT-21', tester_name='Sam Lee', **shared),
        make_item(key='UAT-20', tester_name='Jane Smith', status='Blocked',
                  covering_bug_keys=['UAT-50'], linked_bug_only_keys=['UAT-50'], **shared),
        make_item(key='UAT-22', tester_name='Excluded Person', **shared),
    ]
    defects = {'UAT-50': {'summary': 'Gateway down'}}

    rows = export_by_tester.build_rows(items, defects, changelog_facts={}, source_metadata={})

    assert [row['Tester'] for row in rows] == ['Jane Smith', 'Sam Lee']
    assert {row['Test ID'] for row in rows} == {'UAT-12'}
    assert rows[0]['Blocked Description'] == 'Gateway down'
    assert rows[0]['Defects'] == 'UAT-50'
    assert rows[1]['Blocked Description'] == ''


def test_the_default_priority_is_reported_as_unset():
    item = make_item(source_key='UAT-3')

    rows = export_by_tester.build_rows([item], {}, {}, {'UAT-3': {'priority': 'Medium', 'labels': []}})

    assert rows[0]['Priority'] == ''


def test_generate_writes_the_csv_with_every_column():
    items = [make_item(key='UAT-20', source_key='UAT-3')]

    result = export_by_tester.generate(items=items, session=FakeJira())

    with open(result['path'], encoding='utf-8-sig', newline='') as handle:
        written = list(csv.reader(handle))
    assert written[0] == export_by_tester.EXPORT_COLUMNS
    assert len(written) == 2
    assert result['rowCount'] == 1 and result['testerCount'] == 1
