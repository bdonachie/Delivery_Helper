"""Export live UAT status straight from Jira - no input CSV.

One row per UAT test case (the Task/Subtask each 'Tester' item links to), carrying its
current status, how many of its testing tasks are done, the latest comment, and - where
it is blocked - the defect and that defect's latest comment. Pulled from Jira the same
way every time, so it always reflects the current state.

    python export_uat_status.py
    python export_uat_status.py --output todays_update.csv --no-open

Alongside the detail CSV it writes a summary CSV (roll-ups by area, tester, platform, due
date) and a linked HTML report.
"""

import argparse
import csv
import re
import sys
import webbrowser
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

import requests

import settings
import uat_jira

UAT_REFERENCE_PATTERN = re.compile(r'UAT\s*Ref:\s*([A-Z][A-Z0-9-]*-\d+)', re.IGNORECASE)
TEST_CASE_LINE_PATTERN = re.compile(r'Test Case:\s*(.+)')

# Cancelled work. Jira files these under the 'done' status category, but they are scope that
# was withdrawn, not tests that passed - so they are reported separately from Completed.
CANCELLED_BUCKET = 'Cancelled'
CANCELLED_STATUSES = uat_jira.CANCELLED_STATUSES

# The buckets every roll-up reports, in the order they are shown.
REPORTING_BUCKETS = ['Completed', 'Outstanding', 'Blocked', 'Failed', CANCELLED_BUCKET]

# The export: one row per UAT test case, pulled entirely from Jira. Identifies the test
# case, gives its status and each tester's status, the schedule, why it is blocked (from
# the linked defect / action item), and the latest word from the testers themselves.
# 'Active Testing Tasks' is the denominator that matters day to day - the testers still
# expected to run it - while 'Testing Tasks' stays the full count so a case whose scope was
# cut is not mistaken for one that only ever had a single tester.
UPDATE_COLUMNS = [
    'UAT Test ID', 'Test Case', 'Area', 'UAT Item', 'UAT Item Link', 'UAT Item Status',
    'Testers and Status', 'Testing Progress', 'Testing Tasks', 'Active Testing Tasks',
    'Completed', 'Outstanding', 'Blocked', 'Failed', CANCELLED_BUCKET,
    'Start Date', 'Due Date',
    'Blocked Description', 'Blocked Detail', 'Defect', 'Defect Status',
    'Last Comment', 'Last Comment By', 'Last Comment Date', 'Last Comment On',
    'Linked Bug Defects', 'Linked Bug Summary', 'Linked Bug Description',
]

# Generated exports (detail CSV, summary CSV, HTML report) are written here, one dated set
# per day: uat_status_export_YYYY-MM-DD.csv etc. On the server this is the mounted data
# volume, so exports outlive the container that generated them.
EXPORT_DIRECTORY = settings.EXPORT_DIRECTORY

MAXIMUM_COMMENT_LENGTH = 1000


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description='Export live UAT status straight from Jira - one row per test case.')
    parser.add_argument('--output', default=None,
                        help='CSV to write (default: uat_status_export_<date>.csv in the export directory).')
    parser.add_argument('--no-html', action='store_true',
                        help='Skip the linked HTML report; write the CSVs only.')
    parser.add_argument('--no-open', action='store_true',
                        help='Do not open the HTML report in a browser.')
    return parser.parse_args()


def tidy(value: str) -> str:
    """Make text safe for a CSV cell.

    Collapses whitespace, and converts the inline-image markers carried in Jira comment
    text into readable labels. Those markers are NUL characters, which make a CSV
    unreadable to Excel and to Python's own csv module, so they must never reach the file.
    """
    value = uat_jira.MEDIA_MARKER_PATTERN.sub(
        lambda match: f'[image: {match.group(1)}]', value or '')
    value = value.replace(uat_jira.MEDIA_MARKER, '').replace('\xa0', ' ')
    return ' '.join((value or '').replace(' ', ' ').split()).strip()


def shorten(text: str) -> str:
    text = tidy(text)
    return text if len(text) <= MAXIMUM_COMMENT_LENGTH else text[:MAXIMUM_COMMENT_LENGTH] + '…'


def fetch_defect_details(session: requests.Session,
                         defect_keys: List[str]) -> Dict[str, Dict[str, object]]:
    """Load each blocking defect once, with its comments - that is where the why lives."""
    details: Dict[str, Dict[str, object]] = {}
    for defect_key in sorted(set(defect_keys)):
        response = session.get(
            f'{session.jira_site}/rest/api/3/issue/{defect_key}',
            params={'fields': f'summary,status,issuetype,created,{uat_jira.START_DATE_FIELD},'
                              'description,comment'},
            timeout=uat_jira.REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code >= 400:
            print(f'WARNING: could not read defect {defect_key}: {response.status_code}',
                  file=sys.stderr)
            continue
        fields = response.json().get('fields', {}) or {}
        comments = [uat_jira.build_comment(raw)
                    for raw in (fields.get('comment') or {}).get('comments', [])]
        details[defect_key] = {
            'summary': fields.get('summary', ''),
            'status': (fields.get('status') or {}).get('name', ''),
            'issueType': (fields.get('issuetype') or {}).get('name', ''),
            'description': uat_jira.render_rich_text(fields.get('description') or '').strip(),
            'start_date': fields.get(uat_jira.START_DATE_FIELD) or (fields.get('created') or '')[:10],
            'comments': comments,
        }
    return details


def group_by_uat_case(items: List[uat_jira.TesterItem]) -> Dict[str, List]:
    """Group tester tasks under the UAT test case (linked Task/Subtask) they belong to."""
    grouped: Dict[str, List] = defaultdict(list)
    for item in items:
        if item.source_key:
            grouped[item.source_key].append(item)
    return grouped


def uat_test_id(group: List[uat_jira.TesterItem]) -> str:
    """The UAT-xx id for a test case.

    The reference sits in the test case description on most items, but on some it is only on
    the tester tasks' own descriptions, so both are checked before falling back to the key.
    """
    for item in group:
        match = UAT_REFERENCE_PATTERN.search(item.test_case_detail)
        if match:
            return match.group(1).upper()
    for item in group:
        match = UAT_REFERENCE_PATTERN.search(item.description or '')
        if match:
            return match.group(1).upper()
    return group[0].source_key if group else ''


def uat_test_case_name(group: List[uat_jira.TesterItem]) -> str:
    """The written test case name from the 'Test Case:' line, else the item summary."""
    for item in group:
        match = TEST_CASE_LINE_PATTERN.search(item.test_case_detail)
        if match:
            return tidy(match.group(1))
    return tidy(group[0].source_summary) if group else ''


def uat_area(group: List[uat_jira.TesterItem]) -> str:
    """The epic is the grouping; drop the 'UAT - ' prefix to read as an area."""
    epic = group[0].parent_summary if group else ''
    return re.sub(r'^UAT\s*-\s*', '', epic).strip()


def build_jira_rows(items: List[uat_jira.TesterItem],
                    defect_details: Dict[str, Dict[str, object]],
                    jira_site: str) -> List[List[str]]:
    """One row per UAT test case, pulled entirely from Jira - no CSV involved.

    Cancelled ('WONT DO') tester items are kept rather than dropped. Dropping them used to
    remove whole test cases from the detail export while the summary still counted them, so
    the two files described different populations and could not be reconciled. They are now
    counted in their own column and the case stays visible.
    """
    grouped = group_by_uat_case(items)
    rows: List[List[str]] = []

    for source_key in sorted(grouped):
        group = sorted(grouped[source_key], key=lambda entry: entry.key)

        counts = empty_bucket_counts()
        for item in group:
            counts[status_bucket(item)] += 1
        # What the testers still on the hook for this case are expected to run.
        active_task_count = len(group) - counts[CANCELLED_BUCKET]

        commented = [item for item in group if item.latest_comment]
        newest = max(commented, key=lambda entry: entry.latest_comment.created, default=None)
        latest_comment = newest.latest_comment if newest else None

        # Each tester and their individual status, e.g. "Jane Smith: Blocked; ...".
        testers_and_status = '; '.join(
            f'{item.tester_name}: {item.status}' for item in group if item.tester_name)

        start_dates = sorted({item.effective_start_date for item in group
                              if item.effective_start_date})
        due_dates = sorted({item.effective_due_date for item in group
                            if item.effective_due_date})

        defect_keys: List[str] = []
        for item in group:
            for key in item.covering_bug_keys:
                if key not in defect_keys:
                    defect_keys.append(key)

        # Every Bug linked to any tester in this test case, from ANY epic and regardless of
        # status - the 'Defect' column only shows the open ones under the defect epics, so bugs
        # like a linked dev defect were missed here.
        bug_defect_keys: List[str] = []
        for item in group:
            for key in item.linked_bug_only_keys:
                if key not in bug_defect_keys:
                    bug_defect_keys.append(key)
        # The linked bugs' own title and description (columns Y and Z). Multiple bugs are joined.
        bug_summary = ' | '.join(
            tidy(str(defect_details.get(key, {}).get('summary', ''))) for key in bug_defect_keys)
        bug_description = shorten(' | '.join(
            str(defect_details.get(key, {}).get('description', '')) for key in bug_defect_keys))

        # Why it is blocked: the linked defect / action item's summary, then its full detail.
        blocked_description = blocked_detail = defect_status = ''
        if defect_keys:
            detail = defect_details.get(defect_keys[0], {})
            blocked_description = tidy(str(detail.get('summary', '')))
            blocked_detail = shorten(str(detail.get('description', '')))
            defect_status = tidy(str(detail.get('status', '')))

        rows.append([
            uat_test_id(group),
            uat_test_case_name(group),
            uat_area(group),
            source_key,
            f'{jira_site}/browse/{source_key}',
            group[0].source_status,
            testers_and_status,
            f"{counts['Completed']} of {active_task_count}",
            str(len(group)),
            str(active_task_count),
            str(counts['Completed']), str(counts['Outstanding']),
            str(counts['Blocked']), str(counts['Failed']),
            str(counts[CANCELLED_BUCKET]),
            start_dates[0] if start_dates else '',
            due_dates[-1] if due_dates else '',
            blocked_description,
            blocked_detail,
            ', '.join(defect_keys),
            defect_status,
            shorten(latest_comment.body) if latest_comment else '',
            latest_comment.author if latest_comment else '',
            latest_comment.created[:10] if latest_comment else '',
            newest.key if newest else '',
            ', '.join(bug_defect_keys),
            bug_summary,
            bug_description,
        ])

    # Ordered by UAT Test ID, naturally (UAT-2 before UAT-10, not lexically after it).
    rows.sort(key=lambda row: natural_sort_key(row[UPDATE_COLUMNS.index('UAT Test ID')]))
    return rows


def status_bucket(item: uat_jira.TesterItem) -> str:
    """Collapse workflow statuses into the five that matter for reporting."""
    # Cancelled statuses sit in Jira's 'done' status category, but withdrawn scope is not work
    # completed. They get their own bucket so completion is never overstated - folding them
    # into 'Completed' once reported 764 complete when only 91 tests had actually passed.
    if item.status.strip().lower() in CANCELLED_STATUSES:
        return CANCELLED_BUCKET
    if item.status_category.strip().lower() == uat_jira.FINISHED_STATUS_CATEGORY:
        return 'Completed'
    status = item.status.strip().lower()
    if status == uat_jira.BLOCKED_STATUS:
        return 'Blocked'
    if status == uat_jira.FAILED_STATUS:
        return 'Failed'
    return 'Outstanding'


def empty_bucket_counts() -> Dict[str, int]:
    return {name: 0 for name in ['Total'] + REPORTING_BUCKETS}


def tally(items: List[uat_jira.TesterItem], key_function) -> Dict[str, Dict[str, int]]:
    """Count items by some key, split across the reporting buckets."""
    counts: Dict[str, Dict[str, int]] = defaultdict(empty_bucket_counts)
    for item in items:
        group = key_function(item) or '(none)'
        counts[group]['Total'] += 1
        counts[group][status_bucket(item)] += 1
    return counts


def write_summary_csv(output_path: Path, as_of: date,
                      items: List[uat_jira.TesterItem],
                      area_by_item: Dict[str, str]) -> None:
    overall = tally(items, lambda item: 'All items')['All items']

    with output_path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['UAT status summary'])
        writer.writerow(['As at', as_of.strftime('%Y-%m-%d')])
        writer.writerow([])

        writer.writerow(['Overall', 'Total'] + REPORTING_BUCKETS + ['In Scope'])
        writer.writerow(['All test items', overall['Total']]
                        + [overall[bucket] for bucket in REPORTING_BUCKETS]
                        + [overall['Total'] - overall[CANCELLED_BUCKET]])
        writer.writerow([])

        for title, key_function in [
            ('By area', lambda item: area_by_item.get(item.key, '')),
            ('By tester', lambda item: item.tester_name),
            ('By platform', lambda item: item.platform),
            ('By due date', lambda item: item.effective_due_date),
        ]:
            writer.writerow([title, 'Total'] + REPORTING_BUCKETS + ['In Scope'])
            grouped = tally(items, key_function)
            for group in sorted(grouped, key=natural_sort_key):
                counts = grouped[group]
                writer.writerow([group, counts['Total']]
                                + [counts[bucket] for bucket in REPORTING_BUCKETS]
                                + [counts['Total'] - counts[CANCELLED_BUCKET]])
            writer.writerow([])


def render_html_report(as_of: date, items: List[uat_jira.TesterItem],
                       rows: List[List[str]], area_by_item: Dict[str, str],
                       defect_details: Dict[str, Dict[str, object]],
                       jira_site: str) -> str:
    import html as html_module

    overall = tally(items, lambda item: 'All')['All']
    colours = {'Completed': '#006644', 'Outstanding': '#0747a6',
               'Blocked': '#bf2600', 'Failed': '#bf2600', 'Total': '#172b4d',
               CANCELLED_BUCKET: '#6b778c'}

    headline = ''.join(
        f'<td align="center" style="border:1px solid #dfe1e6;border-radius:6px;'
        f'padding:12px 18px;background:#f4f5f7;">'
        f'<div style="font-size:24px;font-weight:700;color:{colours[bucket]};">'
        f'{overall[bucket]}</div>'
        f'<div style="font-size:11px;text-transform:uppercase;letter-spacing:.6px;'
        f'color:#6b778c;">{bucket}</div></td><td style="width:8px;"></td>'
        for bucket in ['Total'] + REPORTING_BUCKETS)

    def group_table(title: str, key_function) -> str:
        grouped = tally(items, key_function)
        body = ''
        for group in sorted(grouped, key=natural_sort_key):
            counts = grouped[group]
            body += (f'<tr><td>{html_module.escape(str(group))}</td>'
                     f'<td class="n">{counts["Total"]}</td>'
                     f'<td class="n ok">{counts["Completed"]}</td>'
                     f'<td class="n">{counts["Outstanding"]}</td>'
                     f'<td class="n bad">{counts["Blocked"]}</td>'
                     f'<td class="n bad">{counts["Failed"]}</td>'
                     f'<td class="n muted">{counts[CANCELLED_BUCKET]}</td>'
                     f'<td class="n">{counts["Total"] - counts[CANCELLED_BUCKET]}</td></tr>')
        return (f'<h2>{title}</h2><table><thead><tr><th>{title.replace("By ", "").title()}</th>'
                '<th class="n">Total</th><th class="n">Completed</th><th class="n">Outstanding</th>'
                f'<th class="n">Blocked</th><th class="n">Failed</th>'
                f'<th class="n">{CANCELLED_BUCKET}</th><th class="n">In scope</th></tr></thead>'
                f'<tbody>{body}</tbody></table>')

    defect_rows = ''
    blocked_by_defect: Dict[str, int] = defaultdict(int)
    for item in items:
        for key in item.covering_bug_keys:
            blocked_by_defect[key] += 1
    for defect_key, count in sorted(blocked_by_defect.items(), key=lambda pair: -pair[1]):
        detail = defect_details.get(defect_key, {})
        comments = detail.get('comments', []) or []
        latest = comments[-1] if comments else None
        defect_rows += (
            f'<tr><td><a href="{jira_site}/browse/{defect_key}" target="_blank">'
            f'{defect_key}</a></td>'
            f'<td>{html_module.escape(str(detail.get("summary", "")))}</td>'
            f'<td>{html_module.escape(str(detail.get("status", "")))}</td>'
            f'<td class="n bad">{count}</td>'
            f'<td>{html_module.escape(latest.author) if latest else ""}'
            f'<div class="muted">{html_module.escape(shorten(latest.body)) if latest else ""}</div>'
            '</td></tr>')

    detail_rows = ''
    for row in rows:
        record = dict(zip(UPDATE_COLUMNS, row))
        status = record['UAT Item Status']
        lowered = status.strip().lower()
        css = ('bad' if lowered in (uat_jira.BLOCKED_STATUS, uat_jira.FAILED_STATUS)
               else 'ok' if lowered in (uat_jira.DONE_STATUS, 'closed') else '')
        uat_link = (f'<a href="{record["UAT Item Link"]}" target="_blank">'
                    f'{html_module.escape(record["UAT Item"])}</a>'
                    if record['UAT Item'] else '<span class="muted">-</span>')
        defect_link = ' '.join(
            f'<a href="{jira_site}/browse/{key.strip()}" target="_blank">{key.strip()}</a>'
            for key in record['Defect'].split(',') if key.strip())
        detail_rows += (
            f'<tr data-search="{html_module.escape(" ".join(row).lower())}">'
            f'<td>{html_module.escape(record["UAT Test ID"])}</td>'
            f'<td>{html_module.escape(record["Test Case"])[:90]}'
            f'<div class="muted">{html_module.escape(record["Area"])}</div></td>'
            f'<td>{uat_link}</td>'
            f'<td class="{css}">{html_module.escape(status)}</td>'
            f'<td class="n">{html_module.escape(record["Testing Progress"])}</td>'
            f'<td class="n bad">{record["Blocked"]}</td>'
            f'<td>{html_module.escape(record["Due Date"])}</td>'
            f'<td>{defect_link}'
            f'<div class="muted">{html_module.escape(record["Blocked Description"][:110])}</div></td>'
            f'<td>{html_module.escape(record["Last Comment"][:140])}'
            f'<div class="muted">{html_module.escape(record["Last Comment By"])} '
            f'{html_module.escape(record["Last Comment Date"])} '
            f'{html_module.escape(record["Last Comment On"])}</div></td>'
            '</td></tr>')

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>UAT status report {as_of.strftime('%d %b %Y')}</title>
<style>
:root{{color-scheme:light dark;--bg:#f4f5f7;--panel:#fff;--text:#172b4d;--muted:#6b778c;--line:#dfe1e6;--accent:#0052cc;}}
@media(prefers-color-scheme:dark){{:root{{--bg:#15181d;--panel:#1e2228;--text:#e6e8ec;--muted:#98a1b0;--line:#2f343d;--accent:#4c9aff;}}}}
body{{margin:0;padding:24px;background:var(--bg);color:var(--text);font:14px/1.5 -apple-system,"Segoe UI",Roboto,sans-serif;}}
.page{{max-width:1200px;margin:0 auto;}}
h1{{font-size:22px;margin:0 0 4px;}} h2{{font-size:15px;margin:22px 0 6px;}}
.muted{{color:var(--muted);font-size:12px;}}
table{{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden;margin-bottom:8px;}}
th{{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);padding:8px 10px;border-bottom:2px solid var(--line);}}
td{{padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top;}}
td.n,th.n{{text-align:right;}} .ok{{color:#006644;font-weight:600;}} .bad{{color:#bf2600;font-weight:600;}}
a{{color:var(--accent);font-weight:600;text-decoration:none;}} a:hover{{text-decoration:underline;}}
#filter{{width:100%;padding:10px 14px;border:1px solid var(--line);border-radius:8px;background:var(--panel);color:var(--text);font-size:14px;margin:8px 0 14px;}}
</style></head><body><div class="page">
<h1>UAT status report</h1>
<div class="muted">As at {as_of.strftime('%A %d %B %Y')} &middot; {len(rows)} UAT test cases &middot;
{len(items)} testing tasks &middot; pulled live from Jira</div>
<table role="presentation" style="border:none;background:none;margin:16px 0;"><tr>{headline}</tr></table>
<h2>Blocking defects</h2>
<table><thead><tr><th>Defect</th><th>Summary</th><th>Status</th><th class="n">Items blocked</th>
<th>Latest comment</th></tr></thead><tbody>{defect_rows or
  '<tr><td colspan="5" class="muted">No defects linked.</td></tr>'}</tbody></table>
{group_table('By area', lambda item: area_by_item.get(item.key, ''))}
{group_table('By tester', lambda item: item.tester_name)}
{group_table('By due date', lambda item: item.effective_due_date)}
<h2>Every UAT test case: {len(rows)} rows, newest activity first</h2>
<input id="filter" type="search" placeholder="Filter by test id, description, status, tester, defect…">
<table><thead><tr><th>UAT ID</th><th>Test case</th><th>UAT item</th><th>Status</th>
<th class="n">Progress</th><th class="n">Blocked</th><th>Due</th>
<th>Blocked by</th><th>Last comment</th></tr></thead>
<tbody id="detail">{detail_rows}</tbody></table>
</div><script>
const filterInput=document.getElementById('filter');
const detailRows=Array.from(document.querySelectorAll('#detail tr'));
filterInput.addEventListener('input',()=>{{
  const term=filterInput.value.trim().toLowerCase();
  for(const row of detailRows) row.style.display=!term||row.dataset.search.includes(term)?'':'none';
}});
</script></body></html>"""


def natural_sort_key(test_id: str):
    """Sort UAT-9 before UAT-10 rather than lexically."""
    match = re.match(r'^(.*?)(\d+)$', test_id)
    if match:
        return (match.group(1), int(match.group(2)))
    return (test_id, 0)


def generate_export(output: Optional[str] = None,
                    write_html: bool = True,
                    items: Optional[List[uat_jira.TesterItem]] = None,
                    session=None,
                    progress=None) -> Dict[str, object]:
    """Build the export straight from Jira and return what was produced.

    One row per UAT test case, in a fixed format, every time - no input CSV. Shared by the
    command line and the web app. Pass `items` to reuse an already-fetched set, and
    `session` to read as the signed-in user rather than falling back to the environment.
    """
    def report(message: str) -> None:
        if progress:
            progress(message)

    session = session or uat_jira.build_session()

    if items is None:
        report('Fetching UAT test items from Jira...')
        items = uat_jira.fetch_tester_items(session, progress=False)

    # Load details for both the covering defects (Defect column) and every linked Bug (the
    # Linked Bug columns), so their summary/description are available regardless of epic.
    blocking_keys = [key for item in items
                     for key in (item.covering_bug_keys + item.linked_bug_only_keys)]
    report(f'Fetching {len(set(blocking_keys))} blocking defect(s)...')
    defect_details = fetch_defect_details(session, blocking_keys)

    rows = build_jira_rows(items, defect_details, session.jira_site)
    area_by_item = {item.key: uat_area([item]) for item in items}

    # The detail rows are grouped by the linked Task/Subtask, so an item with no linked
    # source cannot appear in them. It still counts in the summary, so say so out loud
    # rather than letting the two files quietly disagree.
    unlinked_items = [item for item in items if not item.source_key]
    if unlinked_items:
        report(f'NOTE: {len(unlinked_items)} tester item(s) have no linked Task or Subtask, '
               f'so they are counted in the summary but cannot appear in the detail rows '
               f'(e.g. {unlinked_items[0].key}).')
    items_without_area = [item for item in items if not area_by_item.get(item.key)]
    if items_without_area:
        report(f'NOTE: {len(items_without_area)} tester item(s) have no epic, so they roll '
               f'up under area "(none)" (e.g. {items_without_area[0].key}).')

    as_of = date.today()
    if output is None:
        # Default: a dated file per day in the export directory.
        output_path = EXPORT_DIRECTORY / f'uat_status_export_{as_of.isoformat()}.csv'
    else:
        output_path = Path(output)
        if not output_path.is_absolute():
            output_path = EXPORT_DIRECTORY / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(UPDATE_COLUMNS)
        writer.writerows(rows)

    summary_path = output_path.with_name(output_path.stem + '_summary.csv')
    write_summary_csv(summary_path, as_of, items, area_by_item)

    html_path = None
    if write_html:
        html_path = output_path.with_suffix('.html')
        html_path.write_text(
            render_html_report(as_of, items, rows, area_by_item, defect_details,
                               session.jira_site),
            encoding='utf-8')

    counts = tally(items, lambda item: 'All').get('All', {})
    return {
        'asOf': as_of.isoformat(),
        'detailPath': str(output_path),
        'summaryPath': str(summary_path),
        'htmlPath': str(html_path) if html_path else '',
        'rowCount': len(rows),
        'columnCount': len(UPDATE_COLUMNS),
        'itemCount': len(items),
        'testCaseCount': len(rows),
        'defectCount': len(defect_details),
        'totals': counts,
        'unlinkedItemCount': len(unlinked_items),
        'itemsWithoutAreaCount': len(items_without_area),
    }


def main() -> int:
    arguments = parse_arguments()

    try:
        result = generate_export(
            output=arguments.output,
            write_html=not arguments.no_html,
            progress=print,
        )
    except (FileNotFoundError, ValueError, requests.RequestException) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 1

    if result['htmlPath'] and not arguments.no_open:
        webbrowser.open(Path(result['htmlPath']).as_uri())

    print(f"\nWritten {result['detailPath']} - {result['rowCount']} test case(s), "
          f"{result['columnCount']} columns.")
    print(f"Written {result['summaryPath']}")
    if result['htmlPath']:
        print(f"Written {result['htmlPath']}")
    print(f"  UAT test cases : {result['testCaseCount']}")
    print(f"  testing tasks  : {result['itemCount']}")
    print(f"  blocking defects: {result['defectCount']}")
    print(f"  totals         : {result['totals']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
