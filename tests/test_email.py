"""Email drafts: the .eml file itself, who gets what, and the summary's history."""

import json
from datetime import date
from email import policy
from email.parser import BytesParser

import email_builder
import email_draft
from factories import make_item


def read_eml(path):
    with open(path, 'rb') as handle:
        return BytesParser(policy=policy.default).parse(handle)


def test_a_draft_opens_as_unsent_with_its_images_inline_and_files_attached(tmp_path):
    image = tmp_path / 'status.png'
    image.write_bytes(b'\x89PNG\r\n\x1a\n')
    guide = tmp_path / 'guide.html'
    guide.write_text('<p>guide</p>', encoding='utf-8')

    path = email_draft.write_draft(
        tmp_path / 'draft.eml', 'Subject line', '<p><img src="cid:status-todo"></p>',
        recipients='jane.smith@example.com',
        attachments=[(guide, 'Testing guide.html'), (tmp_path / 'missing.pdf', 'Missing.pdf')],
        inline_images=[('status-todo', image)])

    message = read_eml(path)
    assert message['X-Unsent'] == '1'
    assert message['To'] == 'jane.smith@example.com'
    parts = list(message.walk())
    assert any(part.get('Content-ID') == '<status-todo>' for part in parts)
    attached = [part.get_filename() for part in message.iter_attachments()]
    assert 'Testing guide.html' in attached
    assert 'Missing.pdf' not in attached  # a missing file costs an attachment, not the draft


def test_testers_get_only_what_is_theirs_to_do_within_the_date_range():
    items = [
        make_item(key='UAT-1', start_date='2026-10-01'),
        make_item(key='UAT-2', start_date='2026-10-09'),
        make_item(key='UAT-3', start_date='2026-10-02', status='Blocked'),
        make_item(key='UAT-4', start_date='2026-10-02', status='Done', status_category='done'),
        make_item(key='UAT-5', start_date='2026-10-02', tester_name=''),
    ]

    grouped = email_builder.group_items_by_tester(items, None, date(2026, 10, 1), date(2026, 10, 5))

    assert {tester: [item.key for item in owned] for tester, owned in grouped.items()} == {'Jane Smith': ['UAT-1']}


def test_a_tester_email_uses_the_configured_wording_and_names_nobody():
    html = email_builder.render_email_html('Jane Smith', [make_item(start_date='2026-10-01')])

    assert 'Hey Jane,' in html
    assert 'within 2 business days' in html
    assert 'Defects are raised by the test leads.' in html
    assert 'Raise questions in #uat-help.' in html
    assert 'Testing guides' not in html  # no guide URL configured, so no link


def test_every_tester_email_carries_the_configured_attachment(tmp_path):
    drafts = email_builder.generate_tester_emails(
        [make_item(start_date='2026-10-01')], {'Jane Smith': 'jane.smith@example.com'}, tmp_path)

    message = read_eml(drafts[0].output_path)
    assert [part.get_filename() for part in message.iter_attachments()] == ['Testing guide.html']
    assert message['Subject'].startswith('UAT test cases to complete - ')


def test_every_cancelled_status_is_left_out_of_the_figures_to_date():
    items = [
        make_item(key='UAT-1', due_date='2026-10-01', status='Done', status_category='done'),
        make_item(key='UAT-2', due_date='2026-10-01', status='Dupe', status_category='done'),
        make_item(key='UAT-3', due_date='2026-10-01', status="Won't Do", status_category='done'),
        make_item(key='UAT-4', due_date='2026-12-01'),  # not due yet
    ]

    in_window = email_builder._to_date_summary_items(items, date(2026, 10, 2))

    assert [item.key for item in in_window] == ['UAT-1']
    assert email_builder.summarise(in_window)['Completed'] == 1


def test_the_summary_compares_against_the_previous_days_snapshot(history_directory):
    (history_directory / 'summary_snapshot_2026-10-01.json').write_text(
        json.dumps({'figures': {'Total': 10, 'Completed': 4, 'Blocked': 3}}), encoding='utf-8')
    today = {'Total': 10, 'Completed': 6, 'Blocked': 1, 'In Progress': 0, 'Outstanding': 3,
             'Failed': 0, 'Retest': 0}

    deltas, previous = email_builder.summary_deltas_since_previous(today, date(2026, 10, 2))

    assert previous == '2026-10-01'
    assert (deltas['Completed'], deltas['Blocked']) == (2, -2)


def test_a_repeat_run_on_the_same_day_does_not_count_as_a_previous_summary(history_directory):
    email_builder.record_summary_snapshot({'Total': 5, 'Completed': 1}, date(2026, 10, 2))

    assert email_builder.summary_deltas_since_previous({'Total': 5}, date(2026, 10, 2)) == (None, None)


def test_the_summary_shows_the_configured_end_date_and_dev_defects_epic(history_directory):
    html = email_builder.render_summary_email_html(
        [make_item(due_date='2026-10-01')], as_of=date(2026, 10, 2), bugs=[],
        dev_defect_items=[{'key': 'UAT-300', 'summary': 'Slow screen', 'status': 'Open',
                           'statusCategory': 'new', 'updated': '2026-10-01', 'url': ''}])

    assert 'UAT ends 18 Dec 2026' in html
    assert 'Development defects' in html and 'raised under UAT-102' in html
    assert (history_directory / 'dev_defects_snapshot_2026-10-02.json').is_file()
