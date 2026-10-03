"""The per-tester export: one row per tester per test case, built fresh from Jira.

A test case with four testers becomes four rows, each with that tester's own status and
dates. Every column comes from Jira, so the file always describes the live state and needs
no input sheet: the planned dates from the schedule, the actual dates from each item's
changelog, the written test case from the linked Task or Subtask, and the defects from the
links.
"""
import csv
import re
from datetime import date, datetime

import export_uat_status
import project
import uat_jira

PROJECT = project.current()

EXPORT_COLUMNS = [
    'Test ID', 'Test Case', 'Area', 'Instrument', 'Platform', 'Test Type', 'Priority',
    'Tester', 'Jira Key', 'Link', 'Status', 'Result',
    'Planned Start Date', 'Planned Completion Date', 'Actual Start Date', 'Actual Completion Date',
    'Pre-Conditions', 'Steps', 'Expected Result',
    'Blocked Description', 'Defects', 'Defect Summary', 'Latest Comment',
]

DATE_COLUMNS = ['Planned Start Date', 'Planned Completion Date',
                'Actual Start Date', 'Actual Completion Date']

# Jira holds the test type as a label on the source work item, e.g. 'type:positive'.
TYPE_LABEL_PREFIX = 'type:'

# 'type:edge' reads better as 'Edge Case', so known values are mapped rather than title-cased.
TEST_TYPE_BY_LABEL_VALUE = {
    'positive': 'Positive',
    'negative': 'Negative',
    'edge': 'Edge Case',
    'regression': 'Regression',
}

# The labelled sections of a test case description, in the order they are written.
TEST_CASE_SECTION_LABELS = ['UAT Ref', 'Test Case', 'Pre-Conditions', 'Functional Steps',
                            'Expected Result', 'Notes']
TEST_CASE_SECTION_PATTERN = re.compile(
    r'^(%s):' % '|'.join(re.escape(label) for label in TEST_CASE_SECTION_LABELS),
    re.MULTILINE)

CHANGELOG_BATCH_SIZE = 80


def result_for(jira_status: str) -> str:
    """The outcome a workflow status amounts to: Pass, Fail, Blocked, In Progress, Pending
    or Not Applicable. Blank for a status that says nothing about the outcome."""
    status = (jira_status or '').strip().lower()
    if status == uat_jira.DONE_STATUS:
        return 'Pass'
    if status == uat_jira.FAILED_STATUS:
        return 'Fail'
    if status == uat_jira.BLOCKED_STATUS:
        return 'Blocked'
    if status in (uat_jira.IN_PROGRESS_STATUS, uat_jira.RETEST_STATUS):
        return 'In Progress'
    if status == uat_jira.TO_DO_STATUS:
        return 'Pending'
    if status in uat_jira.CANCELLED_STATUSES:
        return 'Not Applicable'
    return ''


def format_date(raw_date: str) -> str:
    """Write a Jira date or timestamp in the configured format.

    Anything unrecognised is passed through untouched rather than dropped, so an odd value
    stays visible instead of silently disappearing.
    """
    text = (raw_date or '').strip()
    if not text:
        return ''
    try:
        # An ISO timestamp carries a time after the date; only the date matters here.
        return datetime.strptime(text[:10], '%Y-%m-%d').strftime(PROJECT.export_date_format)
    except ValueError:
        return text


def read_status_changes(histories: list) -> list:
    """(created, from status, to status) for every status transition, oldest first.

    Jira does not promise changelog ordering, so the list is sorted rather than trusted.
    """
    status_changes = []
    for history in histories:
        for change in history.get('items', []):
            if change.get('field') == 'status':
                status_changes.append((
                    history.get('created', ''),
                    (change.get('fromString') or '').strip().lower(),
                    (change.get('toString') or '').strip().lower(),
                ))
    status_changes.sort()
    return status_changes


def find_actual_start_date(status_changes: list) -> str:
    """When work genuinely began: the FIRST time the item left To Do.

    Items get pushed back to To Do and restarted, so the earliest departure is the real
    start - a later one would report the restart and hide how long the test has been open.
    """
    for created, from_status, _ in status_changes:
        if from_status == uat_jira.TO_DO_STATUS:
            return created[:10]
    return ''


def find_actual_completion_date(status_changes: list) -> str:
    """When the item was marked done: the MOST RECENT move into Done.

    Being marked done is what sets the date, so it stands even if the item was later
    reopened - the test was completed on that date, whatever happened afterwards.
    """
    for created, _, to_status in reversed(status_changes):
        if to_status == uat_jira.DONE_STATUS:
            return created[:10]
    return ''


def fetch_changelog_facts(session, keys: list) -> dict:
    """{Jira key -> {actualStartDate, actualCompletionDate}}.

    Changelogs are read in bulk through the JQL search (expand=changelog), so the whole
    export needs a handful of requests rather than one per item.
    """
    facts: dict = {}
    unique_keys = sorted(set(keys))
    for batch_start in range(0, len(unique_keys), CHANGELOG_BATCH_SIZE):
        batch = unique_keys[batch_start:batch_start + CHANGELOG_BATCH_SIZE]
        next_page_token = None
        while True:
            body = {'jql': 'key in (%s)' % ','.join(batch), 'maxResults': 100,
                    'fields': ['status'], 'expand': 'changelog'}
            if next_page_token:
                body['nextPageToken'] = next_page_token
            response = session.post(f'{session.jira_site}/rest/api/3/search/jql',
                                    json=body, timeout=90)
            response.raise_for_status()
            payload = response.json()
            for issue in payload.get('issues', []):
                histories = (issue.get('changelog') or {}).get('histories', [])
                status_changes = read_status_changes(histories)
                facts[issue['key']] = {
                    'actualStartDate': find_actual_start_date(status_changes),
                    'actualCompletionDate': find_actual_completion_date(status_changes),
                }
            next_page_token = payload.get('nextPageToken')
            if not next_page_token or payload.get('isLast'):
                break
    return facts


def fetch_source_metadata(session, source_keys: list) -> dict:
    """{source key -> {'labels': [...], 'priority': str}}, read from the source work items."""
    metadata = {}
    unique_keys = sorted({key for key in source_keys if key})
    for batch_start in range(0, len(unique_keys), CHANGELOG_BATCH_SIZE):
        batch = unique_keys[batch_start:batch_start + CHANGELOG_BATCH_SIZE]
        response = session.post(f'{session.jira_site}/rest/api/3/issue/bulkfetch',
                                json={'issueIdsOrKeys': batch, 'fields': ['labels', 'priority']},
                                timeout=60)
        response.raise_for_status()
        for issue in response.json().get('issues', []):
            fields = issue.get('fields') or {}
            metadata[issue['key']] = {
                'labels': fields.get('labels') or [],
                'priority': (fields.get('priority') or {}).get('name', ''),
            }
    return metadata


def parse_test_case_sections(test_case_detail: str) -> dict:
    """Split a test case description into {label: text} for its labelled sections."""
    sections = {}
    matches = list(TEST_CASE_SECTION_PATTERN.finditer(test_case_detail or ''))
    for position, match in enumerate(matches):
        end = matches[position + 1].start() if position + 1 < len(matches) else len(test_case_detail)
        sections[match.group(1)] = test_case_detail[match.end():end].strip()
    return sections


def test_type_from_labels(labels) -> str:
    """The test type, read from the source's 'type:<value>' label."""
    for label in labels or []:
        if not label.startswith(TYPE_LABEL_PREFIX):
            continue
        value = label[len(TYPE_LABEL_PREFIX):].strip().lower()
        return TEST_TYPE_BY_LABEL_VALUE.get(value, value.replace('-', ' ').title())
    return ''


def format_functional_steps(steps_text: str) -> str:
    """One numbered step per line, which reads far better in a spreadsheet cell than a
    run-on paragraph. Steps that already carry their own number keep it."""
    step_lines = [export_uat_status.tidy(line) for line in (steps_text or '').splitlines()
                  if line.strip()]
    step_lines = [line for line in step_lines if line]
    if not step_lines:
        return ''
    if any(re.match(r'^\d+\.', line) for line in step_lines):
        return '\n'.join(step_lines)
    return '\n'.join(f'{number}. {line}' for number, line in enumerate(step_lines, start=1))


def build_rows(items, defect_details, changelog_facts, source_metadata=None):
    """One row per tester per test case, sorted by test id then tester."""
    grouped = export_uat_status.group_by_uat_case(items)
    # An item with no linked source still gets its own row, so every tester item appears
    # exactly once.
    groups = list(grouped.values()) + [[item] for item in items if not item.source_key]
    excluded_testers = set(PROJECT.export_excluded_testers)

    rows = []
    for group in groups:
        test_id = export_uat_status.uat_test_id(group)
        test_case_detail = next((item.test_case_detail for item in group
                                 if (item.test_case_detail or '').strip()), '')
        sections = parse_test_case_sections(test_case_detail)
        source_key = next((item.source_key for item in group if item.source_key), '')
        source_fields = (source_metadata or {}).get(source_key, {})
        priority = source_fields.get('priority', '')
        if PROJECT.default_priority and priority.strip() == PROJECT.default_priority:
            priority = ''  # the default Jira gives everything is not a real ranking

        for item in sorted(group, key=lambda entry: entry.tester_name or entry.key):
            if (item.tester_name or '') in excluded_testers:
                continue
            linked = item.linked_bug_only_keys or []
            covering = item.covering_bug_keys or []

            def summaries(keys):
                return ' | '.join(
                    export_uat_status.tidy(str(defect_details.get(key, {}).get('summary', '')))
                    for key in keys if defect_details.get(key))

            item_facts = changelog_facts.get(item.key, {})
            row = {
                'Test ID': test_id,
                'Test Case': export_uat_status.uat_test_case_name(group),
                'Area': export_uat_status.uat_area(group),
                'Instrument': item.instrument or '',
                'Platform': item.platform or '',
                'Test Type': test_type_from_labels(source_fields.get('labels')),
                'Priority': priority,
                'Tester': item.tester_name or '',
                'Jira Key': item.key,
                'Link': item.url,
                'Status': item.status or '',
                'Result': result_for(item.status),
                'Planned Start Date': item.effective_start_date or '',
                'Planned Completion Date': item.effective_due_date or '',
                'Actual Start Date': item_facts.get('actualStartDate', ''),
                'Actual Completion Date': item_facts.get('actualCompletionDate', ''),
                'Pre-Conditions': export_uat_status.tidy(sections.get('Pre-Conditions', '')),
                # Already tidied line by line, so the step breaks survive.
                'Steps': format_functional_steps(sections.get('Functional Steps', '')),
                'Expected Result': export_uat_status.tidy(sections.get('Expected Result', '')),
                'Blocked Description': summaries(covering) if item.is_blocked else '',
                'Defects': ', '.join(linked),
                'Defect Summary': summaries(linked),
                'Latest Comment': (export_uat_status.tidy(item.latest_comment.body)
                                   if item.latest_comment else ''),
            }
            for column in DATE_COLUMNS:
                row[column] = format_date(row[column])
            rows.append(row)

    rows.sort(key=lambda row: (export_uat_status.natural_sort_key(row['Test ID']), row['Tester']))
    return rows


def write_rows(path, rows) -> None:
    # utf-8-sig so Excel opens it with the right encoding rather than guessing.
    with path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=EXPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def generate(items=None, session=None) -> dict:
    """Build the export and return where it landed. Reuses `items` when already fetched."""
    session = session or uat_jira.build_session()
    if items is None:
        items = uat_jira.fetch_tester_items(session, progress=False)
    defect_keys = [key for item in items
                   for key in (item.covering_bug_keys + item.linked_bug_only_keys)]
    defect_details = export_uat_status.fetch_defect_details(session, defect_keys)
    changelog_facts = fetch_changelog_facts(session, [item.key for item in items])
    source_metadata = fetch_source_metadata(session, [item.source_key for item in items])
    rows = build_rows(items, defect_details, changelog_facts, source_metadata)

    output_path = (export_uat_status.EXPORT_DIRECTORY
                   / f'tests_by_tester_{date.today().isoformat()}.csv')
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        write_rows(output_path, rows)
    except PermissionError:
        # Today's file is open in Excel and cannot be rewritten; write alongside it instead.
        output_path = output_path.with_name(output_path.stem + '_new.csv')
        write_rows(output_path, rows)

    return {'path': str(output_path), 'rowCount': len(rows),
            'testerCount': len({row['Tester'] for row in rows if row['Tester']}),
            'uatCaseCount': len({row['Test ID'] for row in rows})}


if __name__ == '__main__':
    result = generate()
    print(f'wrote {result["rowCount"]} rows -> {result["path"]}')