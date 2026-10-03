"""Generate .eml email drafts telling each tester which UAT items they own.

Drafts are written as .eml files and downloaded from the web UI, never sent from here.
Each one opens in Outlook as an editable draft. Layout is a greeting, a count, then one
section per start date listing each test case as a link.
"""

import csv
import html as html_module
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import email_draft
import project
import settings
import uat_jira

PROJECT = project.current()

# Optional link shown at the top of every tester email, e.g. a wiki page of known issues.
GUIDE_URL = PROJECT.guide_url

# Files attached to every tester email - access guides, procedures, reference data. They
# belong to one organisation's rollout rather than to the app, so they live in
# config/attachments/ and are listed in config/project.json.
TESTER_EMAIL_ATTACHMENTS = [(settings.ATTACHMENT_DIRECTORY / attachment.file_name,
                             attachment.display_name)
                            for attachment in PROJECT.attachments]

# Status-transition screenshots shown inline in the "How to work your test cases" block of
# every tester email. (content_id, filename, caption)
STATUS_IMAGE_DIRECTORY = settings.EMAIL_IMAGE_DIRECTORY
STATUS_IMAGES = [
    ('status-todo', 'todo.png',
     'Open the status dropdown on the Jira item.'),
    ('status-options', 'Done.png',
     'Choose the status to move to - pick Done when a test passes.'),
    ('status-fail', 'Failed_Blocked_inprogress.png',
     'In Progress, Testing Failed and Blocked transitions.'),
]


@dataclass
class TesterEmail:
    tester_name: str
    tester_email: str
    items: List[uat_jira.TesterItem]
    output_path: Path

    @property
    def item_count(self) -> int:
        return len(self.items)


def parse_flexible_date(raw_value: str) -> Optional[date]:
    """Accept both the dd/mm/yyyy used in the CSVs and the ISO dates the API returns."""
    raw_value = (raw_value or '').strip()
    if not raw_value:
        return None
    for date_format in ('%Y-%m-%d', '%d/%m/%Y'):
        try:
            return datetime.strptime(raw_value, date_format).date()
        except ValueError:
            continue
    return None


def safe_filename_stem(tester_name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', tester_name.lower()).strip('_')


def group_items_by_tester(
    items: Iterable[uat_jira.TesterItem],
    testers_wanted: Optional[Iterable[str]] = None,
    start_of_range: Optional[date] = None,
    end_of_range: Optional[date] = None,
) -> Dict[str, List[uat_jira.TesterItem]]:
    """Bucket eligible items by tester, honouring the date range and status exclusions."""
    wanted = set(testers_wanted) if testers_wanted else None
    items_by_tester: Dict[str, List[uat_jira.TesterItem]] = {}

    for item in items:
        if not item.tester_name:
            continue
        if not item.is_email_eligible:
            continue
        if wanted is not None and item.tester_name not in wanted:
            continue

        scheduled_date = parse_flexible_date(item.effective_start_date)
        if start_of_range and (scheduled_date is None or scheduled_date < start_of_range):
            continue
        if end_of_range and (scheduled_date is None or scheduled_date > end_of_range):
            continue

        items_by_tester.setdefault(item.tester_name, []).append(item)

    return items_by_tester


def build_display_title(item: uat_jira.TesterItem) -> str:
    """'Bonds+Cancel' reads badly in an email; render it as 'Cancel Bonds (TSOX)'."""
    if item.instrument and item.action:
        title = f'{item.action} {item.instrument}'
    else:
        title = item.test_name
    if item.platform and item.platform.lower() not in title.lower():
        title = f'{title} ({item.platform})'
    return title


def _status_pill(label: str, background: str, colour: str) -> str:
    return (f'<span style="display:inline-block;background-color:{background};color:{colour};'
            f'border-radius:3px;padding:1px 7px;font-size:12px;font-weight:700;'
            f'white-space:nowrap;">{label}</span>')


# (background, text) pairs for the status pills.
PILL_GREY = ('#dfe1e6', '#42526e')
PILL_BLUE = ('#deebff', '#0747a6')
PILL_GREEN = ('#e3fcef', '#006644')
PILL_RED = ('#ffebe6', '#bf2600')
PILL_MUTED = ('#f4f5f7', '#6b778c')

# Workflow status -> pill colours, so each item in a tester email shows its live Jira status.
ITEM_STATUS_PILL_COLOURS = {
    uat_jira.TO_DO_STATUS: PILL_GREY,
    uat_jira.IN_PROGRESS_STATUS: PILL_BLUE,
    uat_jira.DONE_STATUS: PILL_GREEN,
    'closed': PILL_GREEN,
    uat_jira.FAILED_STATUS: PILL_RED,
    uat_jira.BLOCKED_STATUS: PILL_RED,
    **{status: PILL_MUTED for status in uat_jira.CANCELLED_STATUSES},
}


def _item_status_pill(item: uat_jira.TesterItem) -> str:
    """A coloured pill showing the item's current Jira status, e.g. 'To Do' or 'Blocked'."""
    background, colour = ITEM_STATUS_PILL_COLOURS.get(
        item.status.strip().lower(), PILL_GREY)
    return _status_pill(item.status, background, colour)


def _instructions_block() -> str:
    """The 'how to work your test cases' guidance shown in every tester email."""
    to_do = _status_pill(html_module.escape(PROJECT.to_do_status), *PILL_GREY)
    in_progress = _status_pill(html_module.escape(PROJECT.in_progress_status), *PILL_BLUE)
    done = _status_pill(html_module.escape(PROJECT.done_status), *PILL_GREEN)
    testing_failed = _status_pill(html_module.escape(PROJECT.failed_status), *PILL_RED)
    blocked = _status_pill(html_module.escape(PROJECT.blocked_status), *PILL_RED)

    item = 'margin:0 0 10px;padding:0;'
    sub = 'margin:6px 0 0 0;padding:0 0 0 18px;color:#42526e;font-size:13px;list-style:disc;'

    def status_figure(content_id: str, caption: str) -> str:
        return (
            '<tr><td style="padding:0 0 12px;">'
            f'<img src="cid:{content_id}" alt="{caption}" '
            'style="display:block;border:1px solid #dfe1e6;max-width:100%;height:auto;">'
            f'<div style="font-size:12px;color:#6b778c;margin-top:4px;">{caption}</div>'
            '</td></tr>')

    figures = ''.join(status_figure(cid, caption) for cid, _name, caption in STATUS_IMAGES)

    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="margin:0 0 20px;"><tr><td style="background-color:#f4f5f7;'
        'border:1px solid #dfe1e6;border-left:4px solid #0052cc;border-radius:6px;'
        'padding:14px 18px;">'
        '<div style="font-weight:700;margin:0 0 10px;">How to work your test cases</div>'
        f'<ol style="margin:0;padding:0 0 0 18px;">'
        f'<li style="{item}">Every test case starts at {to_do}.</li>'
        f'<li style="{item}">When you begin a test, change its status to {in_progress}.</li>'
        f'<li style="{item}">When a test passes, set its status to {done}.</li>'
        f'<li style="{item}">If a test fails, set its status to {testing_failed}.'
        f'<ul style="{sub}"><li>Please add screenshots and any relevant detail in the '
        'comments of the Jira item.</li>'
        '</ul></li>'
        f'<li style="{item}">Any item marked {blocked} must be linked to a defect.'
        f'<ul style="{sub}"><li>Defects are raised by '
        f'{html_module.escape(PROJECT.defects_raised_by)}.</li></ul></li>'
        f'<li style="{item}">If you&rsquo;re unsure of anything, please ask. No question '
        f'is a silly one. Raise questions in {html_module.escape(PROJECT.questions_channel)}.</li>'
        '</ol>'
        '<div style="font-weight:700;margin:14px 0 8px;">Changing a status</div>'
        '<div style="color:#42526e;font-size:13px;margin:0 0 10px;">Open the status '
        'dropdown on the Jira item, then choose the status to move to.</div>'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0">'
        f'{figures}</table>'
        '</td></tr></table>'
    )


def _business_days_phrase(days: int) -> str:
    return f'{days} business day' + ('' if days == 1 else 's')


def render_email_html(tester_name: str, items: List[uat_jira.TesterItem]) -> str:
    first_name = tester_name.split()[0]
    items_by_date: Dict[Optional[date], List[uat_jira.TesterItem]] = {}
    for item in items:
        items_by_date.setdefault(parse_flexible_date(item.effective_start_date), []).append(item)

    sections = [
        '<html><body style="font-family:Segoe UI,Arial,sans-serif;color:#172b4d;'
        'font-size:14px;line-height:1.5;">',
        f'<p style="margin:0 0 12px;">Hey {first_name},</p>',
        '<p style="margin:0 0 14px;">Please see below the test cases that have been prepared '
        "for you. The dates below are the start date, and it's asked that they're completed "
        f'within {_business_days_phrase(PROJECT.testing_window_business_days)} of that.</p>',
        f'<p style="margin:0 0 16px;color:#6b778c;">{len(items)} test case'
        f'{"s" if len(items) != 1 else ""}'
        + (f' &middot; <a href="{html_module.escape(GUIDE_URL, quote=True)}" '
           'style="color:#0052cc;">Testing guides</a>' if GUIDE_URL else '')
        + '</p>',
        _instructions_block(),
    ]

    for scheduled_date in sorted(items_by_date, key=lambda value: (value is None, value)):
        dated_items = items_by_date[scheduled_date]
        heading = scheduled_date.strftime('%A %d %B %Y') if scheduled_date else 'Unscheduled'
        sections.append(
            '<h3 style="background:#f4f5f7;border-left:4px solid #0052cc;padding:6px 10px;'
            f'margin:16px 0 6px;">{heading} '
            f'<span style="color:#6b778c;font-weight:normal;">({len(dated_items)})</span></h3>'
        )
        sections.append('<ul style="margin:0 0 8px;padding-left:20px;">')
        for item in sorted(dated_items, key=build_display_title):
            category_suffix = f' ({item.action})' if item.action else ''
            sections.append(
                '<li style="margin:3px 0;">'
                f'<a href="{item.url}" style="color:#0052cc;text-decoration:none;">'
                f'{build_display_title(item)}</a>{category_suffix} '
                f'{_item_status_pill(item)}</li>'
            )
        sections.append('</ul>')

    sections.append('</body></html>')
    return ''.join(sections)


SUMMARY_BUCKETS = ['Completed', 'In Progress', 'Outstanding', 'Blocked', 'Failed', 'Retest']

# Colour code the status columns across the summary email tables.
STATUS_COLOURS = {
    'Completed': '#006644',    # green
    'In Progress': '#008da6',  # teal
    'Outstanding': '#0747a6',  # blue
    'Blocked': '#bf2600',      # red
    'Failed': '#d97008',       # orange
    'Retest': '#5243aa',       # purple
    'Total': '#172b4d',        # default dark (no highlight)
}


def summary_bucket(item: uat_jira.TesterItem) -> str:
    """Collapse the many workflow statuses into the ones that matter for reporting."""
    status = item.status.strip().lower()
    if item.status_category.strip().lower() == uat_jira.FINISHED_STATUS_CATEGORY:
        return 'Completed'
    if status == uat_jira.BLOCKED_STATUS:
        return 'Blocked'
    if status == uat_jira.FAILED_STATUS:
        return 'Failed'
    if status == uat_jira.RETEST_STATUS:
        return 'Retest'
    if status == uat_jira.IN_PROGRESS_STATUS:
        return 'In Progress'
    return 'Outstanding'


def summarise(items: Iterable[uat_jira.TesterItem]) -> Dict[str, int]:
    counts = {bucket: 0 for bucket in SUMMARY_BUCKETS}
    for item in items:
        counts[summary_bucket(item)] += 1
    counts['Total'] = sum(counts[bucket] for bucket in SUMMARY_BUCKETS)
    return counts


# One snapshot file of the headline figures per day, so each summary email can show how
# every figure has moved since the previous one. Files live in the history directory and are
# named by the date the summary was sent, e.g. summary_snapshot_2026-07-29.json.
SUMMARY_HISTORY_DIRECTORY = settings.HISTORY_DIRECTORY
SUMMARY_SNAPSHOT_PREFIX = 'summary_snapshot_'
HEADLINE_FIGURES = ['Total'] + SUMMARY_BUCKETS


def _snapshot_path(as_of: date) -> Path:
    return SUMMARY_HISTORY_DIRECTORY / f'{SUMMARY_SNAPSHOT_PREFIX}{as_of.isoformat()}.json'


def _snapshot_date_of(snapshot_file: Path) -> Optional[str]:
    """Pull the ISO date out of a snapshot filename, or None if it doesn't fit the pattern."""
    stem = snapshot_file.stem
    if not stem.startswith(SUMMARY_SNAPSHOT_PREFIX):
        return None
    candidate = stem[len(SUMMARY_SNAPSHOT_PREFIX):]
    try:
        datetime.strptime(candidate, '%Y-%m-%d')
    except ValueError:
        return None
    return candidate


def _load_snapshot(snapshot_file: Path) -> Dict[str, int]:
    try:
        return json.loads(snapshot_file.read_text(encoding='utf-8')).get('figures', {})
    except (json.JSONDecodeError, OSError, AttributeError):
        # A corrupt or unreadable snapshot must not stop the email being generated - treat
        # it as absent; the badges simply won't compare against that day.
        return {}


def summary_deltas_since_previous(overall: Dict[str, int],
                                  as_of: date) -> (Optional[Dict[str, int]], Optional[str]):
    """Change in each headline figure since the most recent earlier daily snapshot.

    Scans the History folder for snapshot files dated before today and compares against the
    latest one. Returns (deltas, previous_date_iso); both None when no earlier snapshot
    exists yet (the very first summary, before there is any 'yesterday' to compare against).
    """
    if not SUMMARY_HISTORY_DIRECTORY.is_dir():
        return None, None
    earlier = []
    for snapshot_file in SUMMARY_HISTORY_DIRECTORY.glob(f'{SUMMARY_SNAPSHOT_PREFIX}*.json'):
        snapshot_date = _snapshot_date_of(snapshot_file)
        if snapshot_date and snapshot_date < as_of.isoformat():
            earlier.append((snapshot_date, snapshot_file))
    if not earlier:
        return None, None
    previous_date, previous_file = max(earlier)
    previous = _load_snapshot(previous_file)
    if not previous:
        return None, None
    deltas = {figure: overall.get(figure, 0) - previous.get(figure, 0)
              for figure in HEADLINE_FIGURES}
    return deltas, previous_date


def record_summary_snapshot(overall: Dict[str, int], as_of: date) -> None:
    """Save today's headline figures so a later summary can compare against them.

    One file per day: running the summary again on the same day simply overwrites that
    day's file, so repeat runs never create a spurious 'since an hour ago' comparison.
    """
    SUMMARY_HISTORY_DIRECTORY.mkdir(parents=True, exist_ok=True)
    snapshot = {'date': as_of.isoformat(),
                'figures': {figure: overall.get(figure, 0) for figure in HEADLINE_FIGURES}}
    _snapshot_path(as_of).write_text(json.dumps(snapshot, indent=2, sort_keys=True),
                                     encoding='utf-8')


def _format_snapshot_date(snapshot_date_iso: Optional[str]) -> str:
    """Render a stored ISO snapshot date as 'Mon 28 Jul 2026' for the email caption."""
    if not snapshot_date_iso:
        return 'the previous summary'
    try:
        return datetime.strptime(snapshot_date_iso, '%Y-%m-%d').strftime('%a %d %b %Y')
    except ValueError:
        return snapshot_date_iso


def _to_date_summary_items(items: Iterable[uat_jira.TesterItem],
                           as_of: date) -> List[uat_jira.TesterItem]:
    """The 'to date' window: real UAT effort due on or before the run date.

    Cancelled work is dropped - it is not counted as Completed nor listed as outstanding.
    Every cancelled status counts, not just one of them: a Dupe sits in Jira's 'done'
    category too and would otherwise be reported as a completed test.
    """
    return [item for item in items
            if item.effective_due_date and item.effective_due_date <= as_of.isoformat()
            and item.status.strip().lower() not in uat_jira.CANCELLED_STATUSES]


# --------------------------------------------------------------------------- #
# Defect tracker: the development defects raised during UAT.
#
# Each tracker shows how many items are open, how their statuses break down, and how many
# were created / closed since the previous run. History is kept as one snapshot file per day
# in the same History folder, so created/closed are computed by comparing today's open set
# against the most recent earlier snapshot.
# --------------------------------------------------------------------------- #
def _tracker_snapshot_path(prefix: str, as_of: date) -> Path:
    return SUMMARY_HISTORY_DIRECTORY / f'{prefix}_snapshot_{as_of.isoformat()}.json'


def record_tracker_snapshot(prefix: str, as_of: date,
                            entries: List[Dict[str, str]]) -> None:
    """Save today's open set for a tracker. entries: dicts with 'id','status'.

    One file per day, overwritten on repeat runs, so re-running never invents a change.
    """
    SUMMARY_HISTORY_DIRECTORY.mkdir(parents=True, exist_ok=True)
    by_status: Dict[str, int] = {}
    for entry in entries:
        by_status[entry['status']] = by_status.get(entry['status'], 0) + 1
    snapshot = {'date': as_of.isoformat(), 'count': len(entries), 'byStatus': by_status,
                'ids': sorted(entry['id'] for entry in entries)}
    _tracker_snapshot_path(prefix, as_of).write_text(
        json.dumps(snapshot, indent=2, sort_keys=True), encoding='utf-8')


def tracker_deltas(prefix: str, as_of: date,
                   current_ids: Iterable[str]) -> Optional[Dict[str, object]]:
    """created / closed for a tracker since the most recent earlier snapshot.

    Returns {'created': [...], 'closed': [...], 'previousDate': iso} or None when there is
    no earlier snapshot to compare against.
    """
    if not SUMMARY_HISTORY_DIRECTORY.is_dir():
        return None
    earlier = []
    for snapshot_file in SUMMARY_HISTORY_DIRECTORY.glob(f'{prefix}_snapshot_*.json'):
        snapshot_date = snapshot_file.stem.split('_snapshot_')[-1]
        try:
            datetime.strptime(snapshot_date, '%Y-%m-%d')
        except ValueError:
            continue
        if snapshot_date < as_of.isoformat():
            earlier.append((snapshot_date, snapshot_file))
    if not earlier:
        return None
    previous_date, previous_file = max(earlier)
    try:
        previous = json.loads(previous_file.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return None
    previous_ids = set(previous.get('ids', []))
    current = set(current_ids)
    return {'created': sorted(current - previous_ids),
            'closed': sorted(previous_ids - current),
            'previousDate': previous_date}


# Map any status name to a colour by its meaning, so both SimCorp and Jira statuses read
# consistently without hard-coding every possible label.
def _status_colour(status: str) -> str:
    text = (status or '').strip().lower()
    if any(word in text for word in ('done', 'closed', 'complete', 'resolved')):
        return '#006644'  # green - finished
    if 'progress' in text:
        return '#0052cc'  # blue - being worked
    if any(word in text for word in ('waiting', 'pending', 'hold', 'triage', 'review')):
        return '#a86400'  # amber - waiting on someone
    if any(word in text for word in ('block', 'fail', 'error', 'reject', 'defect')):
        return '#bf2600'  # red - broken
    return '#5e6c84'      # grey - new / unknown


# A soft background tint for each semantic colour, so status chips read as filled pills
# rather than hard outlines. Keyed on the value _status_colour returns.
_STATUS_TINTS = {
    '#006644': '#e4f7ed',   # green
    '#0052cc': '#e4edfb',   # blue
    '#0747a6': '#e4edfb',
    '#008da6': '#e2f4f7',   # teal
    '#a86400': '#fbf1de',   # amber
    '#bf2600': '#fbe7e2',   # red
    '#5e6c84': '#eef0f4',   # grey
}


def _status_palette(status: str):
    """Return (text colour, soft background) for a status chip."""
    foreground = _status_colour(status)
    return foreground, _STATUS_TINTS.get(foreground, '#eef0f4')


def _status_chip_cell(status: str, count: Optional[int] = None) -> str:
    """One status chip as a TABLE CELL.

    Outlook's Word engine ignores display:inline-block / padding / border-radius / background
    on <span>, which collapses span-based pills into run-together coloured text. A <td> with
    a bgcolor attribute and cell padding renders reliably everywhere; spacing between chips
    comes from the table's cellspacing.
    """
    foreground, background = _status_palette(status)
    label = html_module.escape(status or '-')
    count_html = (f' <span style="font-weight:800;">&middot; {count}</span>'
                  if count is not None else '')
    return (f'<td bgcolor="{background}" style="background-color:{background};'
            f'padding:5px 13px;border-radius:12px;font-size:12px;font-weight:600;'
            f'color:{foreground};white-space:nowrap;line-height:1.2;">{label}{count_html}</td>')


def _status_chip_row(status_counts: Dict[str, int]) -> str:
    """The status chips laid out as a spaced single-row table (Outlook-safe)."""
    cells = ''.join(_status_chip_cell(status, count) for status, count
                    in sorted(status_counts.items(), key=lambda pair: -pair[1]))
    return ('<table role="presentation" cellpadding="0" cellspacing="8" border="0" '
            f'style="border-collapse:separate;"><tr>{cells}</tr></table>')


def _tracker_block(title: str, subtitle: str, subtitle_colour: str,
                   entries: List[Dict[str, str]], deltas: Optional[Dict[str, object]],
                   empty_note: str) -> str:
    """A tracker section: heading, then an open-count tile with the status-count pills and
    the created/closed movement. Totals only - individual items are not listed."""
    heading = 'margin:22px 0 4px;font-weight:700;font-size:15px;color:#172b4d;'
    header_html = (f'<p style="{heading}">{html_module.escape(title)} '
                   f'<span style="color:{subtitle_colour};font-size:13px;">('
                   f'{html_module.escape(subtitle)})</span></p>')
    if not entries:
        return header_html + f'<p style="color:#6b778c;margin:0 0 18px;">{empty_note}</p>'

    status_counts: Dict[str, int] = {}
    for entry in entries:
        status_counts[entry['status']] = status_counts.get(entry['status'], 0) + 1
    pills = _status_chip_row(status_counts)

    delta_line = ''
    if deltas is not None:
        since = _format_snapshot_date(deltas['previousDate'])
        delta_line = (
            '<div style="font-size:12px;color:#7a8699;padding-top:9px;">'
            f'<span style="color:#0a7a43;font-weight:700;">+{len(deltas["created"])} created</span>'
            '<span style="color:#c1c7d0;padding:0 8px;">&middot;</span>'
            f'<span style="color:#c23616;font-weight:700;">&minus;{len(deltas["closed"])} closed</span>'
            f'<span style="color:#9aa4b2;">&nbsp; since {html_module.escape(since)}</span></div>')

    tile = (
        '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:4px 0 22px;"><tr>'
        f'<td align="center" style="border:1px solid #e6e9ee;border-left:4px solid {subtitle_colour};'
        'border-radius:10px;padding:13px 26px;background:#fbfcfe;vertical-align:middle;">'
        f'<div style="font-size:30px;font-weight:800;color:{subtitle_colour};line-height:1;">{len(entries)}</div>'
        '<div style="font-size:10px;text-transform:uppercase;letter-spacing:.9px;color:#8993a4;'
        'padding-top:6px;">Open</div></td>'
        f'<td style="padding-left:22px;vertical-align:middle;">{pills}{delta_line}</td>'
        '</tr></table>')

    return header_html + tile


def _dev_defect_entries(dev_defect_items: Optional[List[Dict[str, object]]]) -> List[Dict[str, str]]:
    """Open (not-yet-done) development defects as tracker entries."""
    entries = []
    for defect in dev_defect_items or []:
        if str(defect.get('statusCategory', '')).strip().lower() == 'done':
            continue  # closed defects drop off the open list (counted as 'closed')
        entries.append({'id': defect.get('key', ''), 'title': defect.get('summary', ''),
                        'status': defect.get('status', ''), 'meta': defect.get('updated', ''),
                        'url': defect.get('url', '')})
    return entries


DEV_DEFECTS_SNAPSHOT_PREFIX = 'dev_defects'


def _render_dev_defects_block(dev_defect_items: Optional[List[Dict[str, object]]],
                              as_of: date) -> str:
    """The development-defects tracker, or nothing when no dev defects epic is configured."""
    if not uat_jira.DEV_DEFECTS_EPIC_KEY:
        return ''
    entries = _dev_defect_entries(dev_defect_items)
    deltas = tracker_deltas(DEV_DEFECTS_SNAPSHOT_PREFIX, as_of, [entry['id'] for entry in entries])
    return _tracker_block('Development defects',
                          f'raised under {uat_jira.DEV_DEFECTS_EPIC_KEY}', '#a86400',
                          entries, deltas, 'No open development defects.')


def _delta_badge(change: int) -> str:
    """A small signed figure (+3 / -2) showing the move since the previous summary.

    Colour follows direction, not sentiment: green for a rise, red for a fall. It sits
    under the count so the reader sees the number first and the movement second. Nothing
    is drawn when the figure is unchanged, to keep the tiles clean.
    """
    if not change:
        return ('<div style="font-size:11px;font-weight:600;line-height:1.1;'
                'color:#b3bac5;padding-top:3px;">&plusmn;0</div>')
    sign = '+' if change > 0 else '&minus;'
    colour = '#006644' if change > 0 else '#bf2600'
    return (f'<div style="font-size:11px;font-weight:700;line-height:1.1;'
            f'color:{colour};padding-top:3px;">{sign}{abs(change)}</div>')


def _headline_table(overall: Dict[str, int],
                    deltas: Optional[Dict[str, int]] = None) -> str:
    """Headline figures as a real table.

    Outlook ignores much of the CSS box model, so inline-block pills collapse into a
    ragged column. A table row is the one layout primitive every mail client honours.

    When deltas are supplied, each tile carries a small +/- figure showing how it moved
    since the previous daily summary.
    """
    # Gap between tiles comes from cellspacing, not empty spacer cells - empty cells make
    # some mail clients draw a stray box where the cell sits.
    cells = ''
    for bucket in ['Total'] + SUMMARY_BUCKETS:
        # No +/- on Total (it's just the sum of the others) or Outstanding.
        badge = (_delta_badge(deltas[bucket])
                 if (deltas is not None and bucket not in ('Total', 'Outstanding')) else '')
        cells += (
            '<td align="center" style="border:1px solid #e6e9ee;border-radius:9px;'
            'padding:12px 18px;background-color:#fbfcfe;">'
            f'<div style="font-size:24px;font-weight:800;line-height:1.1;'
            f'color:{STATUS_COLOURS[bucket]};">{overall[bucket]}</div>'
            + badge +
            f'<div style="font-size:10px;text-transform:uppercase;letter-spacing:.7px;'
            f'color:#8993a4;padding-top:5px;">{bucket}</div></td>'
        )
    return (f'<table role="presentation" cellpadding="0" cellspacing="7" '
            f'style="border-collapse:separate;margin:0 0 22px;"><tr>{cells}</tr></table>')


def _summary_table(headings: List[str], rows: List[List[str]], first_column: str) -> str:
    cell = ('padding:6px 10px;border-bottom:1px solid #dfe1e6;'
            'font-size:13px;text-align:right;')
    first = ('padding:6px 10px;border-bottom:1px solid #dfe1e6;'
             'font-size:13px;text-align:left;')
    head = ('padding:6px 10px;border-bottom:2px solid #dfe1e6;font-size:12px;'
            'text-transform:uppercase;letter-spacing:.4px;color:#6b778c;')

    def highlight(heading):
        colour = STATUS_COLOURS.get(heading)
        return f'color:{colour};' if colour and heading != 'Total' else ''

    header_cells = f'<th style="{head}text-align:left;">{first_column}</th>' + ''.join(
        f'<th style="{head}text-align:right;{highlight(heading)}">{heading}</th>'
        for heading in headings)
    body_rows = ''
    for row in rows:
        value_cells = ''.join(
            f'<td style="{cell}{highlight(heading)}'
            f'{"font-weight:600;" if highlight(heading) else ""}">{value}</td>'
            for heading, value in zip(headings, row[1:]))
        body_rows += f'<tr><td style="{first}">{row[0]}</td>{value_cells}</tr>'
    return (f'<table style="border-collapse:collapse;width:100%;margin:6px 0 18px;">'
            f'<thead><tr>{header_cells}</tr></thead><tbody>{body_rows}</tbody></table>')


def _clean_jira_text(value: str) -> str:
    """Strip Jira inline-image markers before text goes into an email.

    render_rich_text wraps inline images in NUL bytes (MEDIA_MARKER). A single NUL reaching
    Outlook's HTMLBody silently corrupts it and truncates the whole email, so the markers are
    turned into readable labels and any stray NUL / non-breaking space is removed - the same
    cleaning the CSV export already does.
    """
    value = value or ''
    value = uat_jira.MEDIA_MARKER_PATTERN.sub(lambda match: f'[image: {match.group(1)}]', value)
    return value.replace(uat_jira.MEDIA_MARKER, '').replace('\xa0', ' ')


def _defects_table(bugs: List[Dict[str, str]], items: List[uat_jira.TesterItem],
                   kind: str) -> str:
    """Open items of ONE type and how many tests each holds up.

    `kind` is 'Bug' (defects - something is broken, from the UAT Defects epic) or 'Action Item'
    (readiness blockers - not broken, just not ready, from the UAT Readiness & Blockers epic).
    The two are shown as separate sections so the email mirrors the Jira structure.
    """
    is_bug = kind != uat_jira.ACTION_ITEM_ISSUE_TYPE
    # A closed item (e.g. a fixed bug) no longer holds up UAT, so drop it from the summary.
    open_bugs = [bug for bug in bugs
                 if bug.get('issueType', 'Bug') == kind
                 and (bug.get('status') or '').strip().lower() not in {'closed', 'done'}]
    if not open_bugs:
        message = 'No open defects raised.' if is_bug else 'No readiness blockers raised.'
        return f'<p style="color:#6b778c;margin:0 0 18px;">{message}</p>'

    linked_count_by_key = {}
    for item in items:
        for defect_key in item.linked_bug_keys:
            linked_count_by_key[defect_key] = linked_count_by_key.get(defect_key, 0) + 1

    cell = 'padding:6px 10px;border-bottom:1px solid #dfe1e6;font-size:13px;'
    head = ('padding:6px 10px;border-bottom:2px solid #dfe1e6;font-size:12px;'
            'text-transform:uppercase;letter-spacing:.4px;color:#6b778c;')
    # Defects red, readiness blockers amber - matching the two lanes in the structure map.
    impact_colour = '#bf2600' if is_bug else '#a86400'

    rows = ''
    for bug in sorted(open_bugs, key=lambda entry: -linked_count_by_key.get(entry['key'], 0)):
        linked = linked_count_by_key.get(bug['key'], 0)
        description = _clean_jira_text((bug.get('description') or '').strip()) or '-'
        if len(description) > 300:
            description = description[:300] + '…'
        # Only real defects (Bugs) have a meaningful workflow status and start date.
        status_cells = ''
        if is_bug:
            status_label = bug['status']
            start_label = bug['start_date'] or '-'
            parsed_start = parse_flexible_date(start_label)
            if parsed_start:
                start_label = parsed_start.strftime('%a %d %b %Y')
                if not bug['has_explicit_start']:
                    start_label += ' (raised)'
            status_cells = (
                f'<td style="{cell}vertical-align:top;">{html_module.escape(status_label)}</td>'
                f'<td style="{cell}vertical-align:top;white-space:nowrap;">{start_label}</td>')
        # Defects list Item/Summary/Description/Status/Start date only. The tests-held-up count
        # is the defining metric for a readiness bucket, so the count column shows there only.
        impact_cell = '' if is_bug else (
            f'<td style="{cell}vertical-align:top;text-align:right;color:{impact_colour};'
            f'font-weight:{"700" if linked else "400"};">{linked or "-"}</td>')
        rows += (
            '<tr>'
            f'<td style="{cell}vertical-align:top;"><a href="{bug["url"]}" '
            f'style="color:#0052cc;font-weight:600;text-decoration:none;">{bug["key"]}</a></td>'
            f'<td style="{cell}vertical-align:top;font-weight:600;">'
            f'{html_module.escape(_clean_jira_text(bug["summary"]))}</td>'
            f'<td style="{cell}vertical-align:top;color:#42526e;">'
            f'{html_module.escape(description)}</td>'
            f'{status_cells}{impact_cell}'
            '</tr>'
        )

    status_headers = (f'<th style="{head}text-align:left;">Status</th>'
                      f'<th style="{head}text-align:left;">Start date</th>') if is_bug else ''
    impact_header = ('' if is_bug else
                     f'<th style="{head}text-align:right;color:{impact_colour};">Tests Held Up</th>')
    return (f'<table style="border-collapse:collapse;width:100%;margin:6px 0 18px;">'
            f'<thead><tr>'
            f'<th style="{head}text-align:left;">Item</th>'
            f'<th style="{head}text-align:left;">Summary</th>'
            f'<th style="{head}text-align:left;">Description</th>'
            f'{status_headers}'
            f'{impact_header}'
            f'</tr></thead><tbody>{rows}</tbody></table>')


# --------------------------------------------------------------------------- #
# Burn-down: are we on track to finish UAT by the deadline?
# --------------------------------------------------------------------------- #
# The date the round must finish by (config/project.json). No date, no banner.
UAT_END_DATE = PROJECT.end_date


def _working_days_between(start_day: date, end_day: date) -> int:
    """Inclusive count of Mon-Fri days from start_day to end_day."""
    day, count = start_day, 0
    while day <= end_day:
        if day.weekday() < 5:
            count += 1
        day += timedelta(days=1)
    return count


def _recent_completion_rate(current_completed: int, as_of: date):
    """Average completions per working day over roughly the last week, from the daily
    snapshots. Returns (rate, delta, working_days, from_iso) or None with no earlier snapshot."""
    if not SUMMARY_HISTORY_DIRECTORY.is_dir():
        return None
    earlier = []
    for snapshot_file in SUMMARY_HISTORY_DIRECTORY.glob('summary_snapshot_*.json'):
        stamp = snapshot_file.stem.split('_snapshot_')[-1]
        try:
            snapshot_date = datetime.strptime(stamp, '%Y-%m-%d').date()
        except ValueError:
            continue
        if snapshot_date < as_of:
            earlier.append((snapshot_date, snapshot_file))
    if not earlier:
        return None
    window_start = as_of - timedelta(days=8)
    in_window = [pair for pair in earlier if pair[0] >= window_start] or earlier
    from_date, from_file = min(in_window)
    try:
        completed_then = json.loads(from_file.read_text(encoding='utf-8'))['figures']['Completed']
    except (json.JSONDecodeError, OSError, KeyError):
        return None
    working_days = _working_days_between(from_date + timedelta(days=1), as_of)
    if working_days <= 0:
        return None
    return ((current_completed - completed_then) / working_days,
            current_completed - completed_then, working_days, from_date.isoformat())


def _burndown_banner(overall: Dict[str, int], to_date_items: List[uat_jira.TesterItem],
                     as_of: date) -> str:
    """A prominent 'are we on track?' banner: days left, required vs actual rate, verdict.

    Omitted when no end date is configured - without a deadline there is nothing to be on
    track for.
    """
    if UAT_END_DATE is None:
        return ''
    working_left = _working_days_between(as_of + timedelta(days=1), UAT_END_DATE)
    open_count = overall['Total'] - overall['Completed']
    required = (open_count / working_left) if working_left else 0.0
    rate_info = _recent_completion_rate(overall['Completed'], as_of)
    actual = rate_info[0] if rate_info else None

    if actual is None:
        tag, colour, background = 'Tracking', '#5e6c84', '#eef0f4'
    elif actual >= required:
        tag, colour, background = 'On track', '#1a7f4b', '#e4f7ed'
    elif actual >= required * 0.7:
        tag, colour, background = 'At risk', '#a8640c', '#fbf1de'
    else:
        tag, colour, background = "Behind, won't land at current pace", '#c0392b', '#fbe7e2'

    def stat(value, label, value_colour='#172b4d'):
        return (f'<td align="center" style="padding:6px 16px 6px 0;">'
                f'<div style="font-size:22px;font-weight:800;color:{value_colour};'
                f'line-height:1;">{value}</div>'
                f'<div style="font-size:10px;text-transform:uppercase;letter-spacing:.5px;'
                f'color:#6b778c;padding-top:4px;">{label}</div></td>')

    actual_txt = f'~{actual:.0f}' if actual is not None else 'n/a'
    stats = (
        '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:10px 0 8px;"><tr>'
        + stat(working_left, 'Working days left')
        + stat(f'{open_count:,}', 'Still open')
        + stat(f'{required:.0f} / day', 'Needed to finish', '#c0392b')
        + stat(f'{actual_txt} / day', 'Actual (recent)', '#a8640c')
        + '</tr></table>')

    heading = 'margin:8px 0 4px;font-weight:700;font-size:15px;color:#172b4d;'
    return (
        f'<p style="{heading}">Are we on track?</p>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="margin:0 0 20px;border-collapse:separate;"><tr>'
        f'<td bgcolor="{background}" style="background-color:{background};border:1px solid {colour};'
        f'border-left:5px solid {colour};border-radius:10px;padding:14px 18px;">'
        f'<span style="display:inline-block;background:#ffffff;color:{colour};border-radius:20px;'
        f'padding:3px 12px;font-size:12px;font-weight:800;text-transform:uppercase;'
        f'letter-spacing:.04em;">{tag}</span>'
        f'&nbsp;&nbsp;&nbsp;'
        f'<span style="color:#6b778c;font-size:12px;">UAT ends '
        f'{UAT_END_DATE.strftime("%d %b %Y")}</span>'
        f'{stats}'
        f'</td></tr></table>')


def _defects_and_actions_table(bugs: List[Dict[str, str]],
                               items: List[uat_jira.TesterItem]) -> str:
    """Open defects and action items, straight from Jira, sorted by how many tests each holds
    up. This is the 'what's blocking / where action is needed' view."""
    open_defects = [bug for bug in bugs
                    if bug.get('status_category', '').strip().lower() != 'done'
                    and (bug.get('status') or '').strip().lower() not in {'closed', 'done'}]
    if not open_defects:
        return '<p style="color:#6b778c;margin:0 0 18px;">No open defects or action items.</p>'

    impacted = {}
    for item in items:
        for key in (item.linked_bug_keys or []):
            impacted[key] = impacted.get(key, 0) + 1
    open_defects.sort(key=lambda bug: -impacted.get(bug['key'], 0))

    cell = 'padding:7px 10px;border-bottom:1px solid #e3e7ee;font-size:13px;vertical-align:top;'
    head = ('padding:7px 10px;border-bottom:2px solid #e3e7ee;font-size:11px;'
            'text-transform:uppercase;letter-spacing:.4px;color:#6b778c;text-align:left;')
    rows = ''
    for bug in open_defects:
        count = impacted.get(bug['key'], 0)
        description = _clean_jira_text((bug.get('description') or '').strip()) or '-'
        if len(description) > 220:
            description = description[:220] + '&hellip;'
        is_bug = bug.get('issueType', '') == 'Bug'
        type_colour = '#bf2600' if is_bug else '#a8640c'
        count_colour = '#c0392b' if count >= 20 else ('#172b4d' if count else '#8993a4')
        rows += (
            '<tr>'
            f'<td style="{cell}white-space:nowrap;"><a href="{bug["url"]}" '
            f'style="color:#0052cc;font-weight:700;text-decoration:none;">{bug["key"]}</a></td>'
            f'<td style="{cell}white-space:nowrap;color:{type_colour};font-weight:600;">'
            f'{html_module.escape(bug.get("issueType",""))}</td>'
            f'<td style="{cell}font-weight:700;">{html_module.escape(bug.get("summary",""))}</td>'
            f'<td style="{cell}color:#6b778c;">{description}</td>'
            f'<td style="{cell}white-space:nowrap;">{html_module.escape(bug.get("status",""))}</td>'
            f'<td style="{cell}text-align:right;white-space:nowrap;font-weight:800;'
            f'color:{count_colour};">{count or "-"}</td>'
            '</tr>')
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;margin:4px 0 18px;">'
        f'<tr><th style="{head}">Item</th><th style="{head}">Type</th>'
        f'<th style="{head}">Summary</th><th style="{head}">Description</th>'
        f'<th style="{head}">Status</th><th style="{head}text-align:right;">Tests impacted</th></tr>'
        f'{rows}</table>')


def render_summary_email_html(items: List[uat_jira.TesterItem],
                              as_of: Optional[date] = None,
                              bugs: Optional[List[Dict[str, str]]] = None,
                              dev_defect_items: Optional[List[Dict[str, object]]] = None) -> str:
    """A management view: the development defect tracker, the defects raised, the headline
    totals, then a breakdown by date and tester.

    The totals and breakdowns cover test cases due from the first one up to the run date -
    the picture 'to date'. The Defects table counts every linked test regardless of date,
    so it reflects each item's full impact. `dev_defect_items` are the dev defects epic's
    children.
    """
    as_of = as_of or date.today()
    bugs = bugs or []

    # 'To date' window: everything due on or before the day the report is run. Cancelled work
    # is dropped - it is not real UAT effort, so it should neither be counted as
    # Completed (its Jira category is 'done') nor list testers who are no longer participating.
    to_date_items = _to_date_summary_items(items, as_of)
    overall = summarise(to_date_items)
    deltas, previous_snapshot_date = summary_deltas_since_previous(overall, as_of)

    testers = sorted({item.tester_name for item in to_date_items if item.tester_name})
    dates = sorted({item.effective_due_date for item in to_date_items
                    if item.effective_due_date})
    headline = _headline_table(overall, deltas)

    # By due date.
    date_rows = []
    for due_date in dates:
        dated = [item for item in to_date_items if item.effective_due_date == due_date]
        counts = summarise(dated)
        label = parse_flexible_date(due_date)
        # An overdue item is any still-open work whose due date has passed - outstanding,
        # in progress or awaiting a retest.
        overdue = (' (overdue)' if label and label < as_of
                   and (counts['Outstanding'] or counts['In Progress'] or counts['Retest'])
                   else '')
        date_rows.append([
            (label.strftime('%a %d %b %Y') if label else due_date) + overdue,
            counts['Total'], counts['Completed'], counts['In Progress'],
            counts['Outstanding'], counts['Blocked'], counts['Failed'], counts['Retest'],
        ])

    # By tester.
    tester_rows = []
    for tester_name in testers:
        owned = [item for item in to_date_items if item.tester_name == tester_name]
        counts = summarise(owned)
        tester_rows.append([
            tester_name, counts['Total'], counts['Completed'], counts['In Progress'],
            counts['Outstanding'], counts['Blocked'], counts['Failed'], counts['Retest'],
        ])
    # Sort by the Outstanding column (now index 4 after inserting In Progress).
    tester_rows.sort(key=lambda row: -row[4])

    columns = ['Total', 'Completed', 'In Progress', 'Outstanding', 'Blocked', 'Failed', 'Retest']
    heading = 'margin:22px 0 4px;font-weight:700;font-size:15px;color:#172b4d;'

    content = (
        f'<div style="font-size:20px;font-weight:700;">UAT status summary</div>'
        f'<div style="margin:2px 0 4px;color:#6b778c;font-size:13px;">'
        f'As at {as_of.strftime("%A %d %B %Y")} '
        f'&middot; {overall["Total"]} test cases due to date across {len(testers)} testers</div>'
        # "Are we on track?" burn-down verdict, right at the top - the report does the maths.
        + _burndown_banner(overall, to_date_items, as_of) +
        # What's blocking / where action is needed - straight from Jira, sorted by impact.
        f'<p style="{heading}margin-top:6px;">Defects &amp; action items '
        '<span style="color:#bf2600;font-size:13px;">(what needs clearing)</span></p>'
        + _defects_and_actions_table(bugs, items) +
        # Development defects raised during UAT.
        _render_dev_defects_block(dev_defect_items, as_of) +
        f'<p style="{heading}">Overall to date</p>'
        + (f'<div style="margin:-2px 0 6px;color:#6b778c;font-size:12px;">'
           f'Small <span style="color:#006644;font-weight:700;">+</span>/'
           f'<span style="color:#bf2600;font-weight:700;">&minus;</span> figures show the '
           f'change since the previous summary ({_format_snapshot_date(previous_snapshot_date)}).'
           f'</div>' if deltas is not None else '')
        + headline +
        f'<p style="{heading}">By due date</p>'
        + _summary_table(columns, date_rows, 'Due date') +
        f'<p style="{heading}">By tester</p>'
        + _summary_table(columns, tester_rows, 'Tester') +
        '<p style="color:#6b778c;font-size:12px;margin-top:16px;">Totals and breakdowns '
        'cover test cases due up to the run date; anything due later is not yet counted. '
        'Outstanding is work not yet started; in progress, blocked, failed and retest are '
        'each counted separately. Cancelled items are excluded entirely. Tests '
        'Impacted counts every linked test regardless of date. Generated from Jira.</p>'
    )

    # Record today's figures so the +/- movement builds up day over day. Done on every
    # render (preview and send) so the baseline is set reliably the moment the summary is
    # looked at, rather than only on a full generate. Same-day renders just overwrite today.
    record_summary_snapshot(overall, as_of)
    if uat_jira.DEV_DEFECTS_EPIC_KEY:
        record_tracker_snapshot(DEV_DEFECTS_SNAPSHOT_PREFIX, as_of,
                                _dev_defect_entries(dev_defect_items))

    # Wrapped in a table-based card so it renders consistently in Outlook and webmail:
    # a light backdrop, a centred white panel with a coloured top bar.
    return (
        '<html><body style="margin:0;padding:0;background-color:#eef1f5;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background-color:#eef1f5;"><tr><td align="center" style="padding:24px 12px;">'
        '<table role="presentation" width="900" cellpadding="0" cellspacing="0" '
        'style="max-width:900px;width:100%;background-color:#ffffff;border:1px solid #dfe1e6;'
        'border-top:4px solid #0052cc;border-radius:8px;">'
        '<tr><td style="padding:24px 28px;font-family:Segoe UI,Arial,sans-serif;'
        f'color:#172b4d;font-size:14px;line-height:1.5;">{content}</td></tr>'
        '</table></td></tr></table></body></html>'
    )


def generate_summary_email(items: List[uat_jira.TesterItem], output_directory: Path,
                           recipients: str, subject: Optional[str] = None,
                           as_of: Optional[date] = None,
                           bugs: Optional[List[Dict[str, str]]] = None,
                           dev_defect_items: Optional[List[Dict[str, object]]] = None) -> Path:
    """Write the summary as a .eml draft ready to download. Nothing is sent."""
    as_of = as_of or date.today()
    return email_draft.write_draft(
        output_path=output_directory / 'uat_status_summary.eml',
        subject=subject or f'UAT status summary - {as_of.strftime("%d %b %Y")}',
        html_body=render_summary_email_html(items, as_of, bugs, dev_defect_items),
        recipients=recipients,
    )


# Colour code compliance rule statuses in the compliance summary email.
COMPLIANCE_STATUS_COLOURS = {
    'Done': '#006644',          # green - signed off
    'Closed': '#006644',
    'UAT': '#0747a6',           # blue - in UAT
    'In Progress': '#008da6',   # teal
    'To Do': '#5e6c84',         # grey - not started
}
# The order statuses are shown in, most-progressed first.
COMPLIANCE_STATUS_ORDER = ['Done', 'Closed', 'UAT', 'In Progress', 'To Do']


def compliance_rule_number(summary: str) -> str:
    """The rule code before the first underscore, e.g. 'Simcorp - 100567_OTHER...' -> '100567'.

    Mirrors the GUI's complianceRuleNumber: strip an optional 'Simcorp - ' prefix, take the
    digits before the first underscore, and leave assignment/report rows (no number) blank.
    """
    text = re.sub(r'^\s*simcorp\s*-\s*', '', summary or '', flags=re.IGNORECASE)
    match = re.search(r'(\d+)_', text)
    return match.group(1) if match else ''


# Jira status pill colours (background, text) for the compliance status bubble.
COMPLIANCE_STATUS_PILL = {
    'uat': ('#deebff', '#0747a6'),
    'in progress': ('#e2f6f9', '#067a8c'),
    'to do': ('#dfe1e6', '#42526e'),
    'done': ('#e3fcef', '#006644'),
    'closed': ('#e3fcef', '#006644'),
}


def _compliance_status_pill(status: str) -> str:
    background, colour = COMPLIANCE_STATUS_PILL.get(status.strip().lower(), ('#dfe1e6', '#42526e'))
    return _status_pill(status, background, colour)


def _compliance_instructions_block() -> str:
    """How to work the compliance rules in Jira - the guidance shown at the top of the email."""
    to_do = _status_pill('To Do', '#dfe1e6', '#42526e')
    in_progress = _status_pill('In Progress', '#e2f6f9', '#067a8c')
    uat = _status_pill('UAT', '#deebff', '#0747a6')
    done = _status_pill('Done', '#e3fcef', '#006644')
    item = 'margin:0 0 9px;padding:0;'
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="margin:0 0 22px;"><tr><td style="background-color:#f4f5f7;'
        'border:1px solid #dfe1e6;border-left:4px solid #0052cc;border-radius:6px;'
        'padding:14px 18px;">'
        '<div style="font-weight:700;margin:0 0 8px;">How to work the compliance rules</div>'
        '<div style="color:#42526e;font-size:13px;margin:0 0 10px;">Each rule below is a Jira '
        'item, so please keep its status current as you work through it.</div>'
        '<ol style="margin:0;padding:0 0 0 18px;">'
        f'<li style="{item}">Every rule starts at {to_do}.</li>'
        f'<li style="{item}">When you begin checking a rule, move it to {in_progress}.</li>'
        f'<li style="{item}">Once the rule has been validated, set it to {uat}.</li>'
        f'<li style="{item}">When it is signed off, move it to {done}.</li>'
        f'<li style="{item}">Record what you checked, and any evidence, in the '
        'rule&rsquo;s comments. If a rule cannot be validated, add a comment explaining why.</li>'
        f'<li style="{item}">If you&rsquo;re unsure of anything, please ask. Raise questions '
        f'in {html_module.escape(PROJECT.questions_channel)}.</li>'
        '</ol>'
        '<div style="color:#42526e;font-size:13px;margin:10px 0 0;">To change a status, open the '
        'status dropdown on the Jira item and choose the new status.</div>'
        '</td></tr></table>'
    )


def _compliance_group_table(rows: List[tuple]) -> str:
    """One rule per row: key, rule number, rule name, status. `rows` = (key, url, number, name, status)."""
    cell = 'padding:5px 10px;border-bottom:1px solid #dfe1e6;font-size:13px;vertical-align:top;'
    head = ('padding:5px 10px;border-bottom:2px solid #dfe1e6;font-size:12px;'
            'text-transform:uppercase;letter-spacing:.4px;color:#6b778c;text-align:left;')
    body = ''
    for key, url, number, name, status in rows:
        body += (
            '<tr>'
            f'<td style="{cell}white-space:nowrap;"><a href="{url}" '
            f'style="color:#0052cc;font-weight:600;text-decoration:none;white-space:nowrap;">'
            f'{key}</a></td>'
            f'<td style="{cell}white-space:nowrap;font-weight:600;">{number or "-"}</td>'
            f'<td style="{cell}">{html_module.escape(name)}</td>'
            f'<td style="{cell}white-space:nowrap;">{_compliance_status_pill(status)}</td>'
            '</tr>')
    return (f'<table style="border-collapse:collapse;width:100%;margin:4px 0 16px;">'
            f'<thead><tr><th style="{head}">Key</th><th style="{head}">Rule #</th>'
            f'<th style="{head}">Rule</th><th style="{head}">Status</th></tr></thead>'
            f'<tbody>{body}</tbody></table>')


def render_compliance_email_html(items: List[Dict[str, object]],
                                 as_of: Optional[date] = None) -> str:
    """Every compliance rule under the Compliance epic, grouped by status."""
    as_of = as_of or date.today()

    status_counts: Dict[str, int] = {}
    for item in items:
        status_counts[item['status']] = status_counts.get(item['status'], 0) + 1
    ordered_statuses = ([s for s in COMPLIANCE_STATUS_ORDER if status_counts.get(s)]
                        + sorted(s for s in status_counts if s not in COMPLIANCE_STATUS_ORDER))

    # Headline tiles: Total, then one per status present.
    tiles = ''
    for label, count, colour in ([('Total', len(items), '#172b4d')]
                                 + [(status, status_counts[status],
                                     COMPLIANCE_STATUS_COLOURS.get(status, '#42526e'))
                                    for status in ordered_statuses]):
        tiles += (
            '<td align="center" style="border:1px solid #dfe1e6;border-radius:6px;'
            'padding:12px 18px;background-color:#f4f5f7;">'
            f'<div style="font-size:24px;font-weight:700;line-height:1.1;color:{colour};">'
            f'{count}</div>'
            f'<div style="font-size:11px;text-transform:uppercase;letter-spacing:.6px;'
            f'color:#6b778c;padding-top:4px;">{label}</div></td>')
    headline = (f'<table role="presentation" cellpadding="0" cellspacing="6" '
                f'style="border-collapse:separate;margin:0 0 22px;"><tr>{tiles}</tr></table>')

    heading = 'margin:22px 0 4px;font-weight:700;font-size:15px;color:#172b4d;'

    def sort_key(item):
        number = compliance_rule_number(item['summary'])
        return (0, int(number)) if number.isdigit() else (1, 0, item['key'])

    groups_html = ''
    for status in ordered_statuses:
        group = sorted((item for item in items if item['status'] == status), key=sort_key)
        rows = [(item['key'], item['url'], compliance_rule_number(item['summary']),
                 _clean_jira_text(item['summary']), item['status']) for item in group]
        colour = COMPLIANCE_STATUS_COLOURS.get(status, '#42526e')
        groups_html += (
            f'<p style="{heading}"><span style="color:{colour};">{html_module.escape(status)}</span> '
            f'<span style="color:#6b778c;font-weight:normal;font-size:13px;">'
            f'({len(group)})</span></p>' + _compliance_group_table(rows))

    content = (
        '<div style="font-size:20px;font-weight:700;">Compliance rules summary</div>'
        f'<div style="margin:2px 0 4px;color:#6b778c;font-size:13px;">'
        f'As at {as_of.strftime("%A %d %B %Y")} &middot; {len(items)} rules under '
        f'{uat_jira.COMPLIANCE_EPIC_KEY} (excluding Won&rsquo;t Do)</div>'
        f'<p style="{heading}">Overall</p>' + headline
        + _compliance_instructions_block() + groups_html +
        '<p style="color:#6b778c;font-size:12px;margin-top:16px;">The rule number is the code '
        'before the first underscore in each rule summary. Generated from Jira.</p>'
    )

    return (
        '<html><body style="margin:0;padding:0;background-color:#eef1f5;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background-color:#eef1f5;"><tr><td align="center" style="padding:24px 12px;">'
        '<table role="presentation" width="900" cellpadding="0" cellspacing="0" '
        'style="max-width:900px;width:100%;background-color:#ffffff;border:1px solid #dfe1e6;'
        'border-top:4px solid #0052cc;border-radius:8px;">'
        '<tr><td style="padding:24px 28px;font-family:Segoe UI,Arial,sans-serif;'
        f'color:#172b4d;font-size:14px;line-height:1.5;">{content}</td></tr>'
        '</table></td></tr></table></body></html>'
    )


def generate_compliance_email(items: List[Dict[str, object]], output_directory: Path,
                              recipients: str, subject: Optional[str] = None,
                              as_of: Optional[date] = None) -> Path:
    """Write the compliance summary as a .eml draft ready to download. Nothing is sent."""
    as_of = as_of or date.today()
    return email_draft.write_draft(
        output_path=output_directory / 'compliance_summary.eml',
        subject=subject or f'Compliance rules summary - {as_of.strftime("%d %b %Y")}',
        html_body=render_compliance_email_html(items, as_of),
        recipients=recipients,
    )


def _email_card(content: str) -> str:
    """Wrap body content in the shared white-card email shell (renders well in Outlook)."""
    return (
        '<html><body style="margin:0;padding:0;background-color:#eef1f5;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background-color:#eef1f5;"><tr><td align="center" style="padding:24px 12px;">'
        '<table role="presentation" width="900" cellpadding="0" cellspacing="0" '
        'style="max-width:900px;width:100%;background-color:#ffffff;border:1px solid #dfe1e6;'
        'border-top:4px solid #0052cc;border-radius:8px;">'
        '<tr><td style="padding:24px 28px;font-family:Segoe UI,Arial,sans-serif;'
        f'color:#172b4d;font-size:14px;line-height:1.5;">{content}</td></tr>'
        '</table></td></tr></table></body></html>'
    )


def render_tester_breakdown_email_html(items: List[uat_jira.TesterItem],
                                       as_of: Optional[date] = None) -> str:
    """Every test case, grouped by tester, one row each: test case, platform, status, due, defect."""
    as_of = as_of or date.today()

    by_tester: Dict[str, List[uat_jira.TesterItem]] = {}
    for item in items:
        if item.tester_name:
            by_tester.setdefault(item.tester_name, []).append(item)

    cell = 'padding:5px 10px;border-bottom:1px solid #dfe1e6;font-size:13px;vertical-align:top;'
    head = ('padding:5px 10px;border-bottom:2px solid #dfe1e6;font-size:12px;'
            'text-transform:uppercase;letter-spacing:.4px;color:#6b778c;text-align:left;')
    heading = 'margin:22px 0 4px;font-weight:700;font-size:15px;color:#172b4d;'

    total = sum(len(group) for group in by_tester.values())
    groups_html = ''
    for tester_name in sorted(by_tester):
        group = sorted(by_tester[tester_name],
                       key=lambda item: (item.effective_due_date or '9999-99-99',
                                         build_display_title(item)))
        body = ''
        for item in group:
            defect = ', '.join(item.covering_bug_keys) or '-'
            due = item.effective_due_date or '-'
            body += (
                '<tr>'
                f'<td style="{cell}"><a href="{item.url}" '
                f'style="color:#0052cc;font-weight:600;text-decoration:none;">{item.key}</a></td>'
                f'<td style="{cell}">'
                f'{html_module.escape(_clean_jira_text(build_display_title(item)))}</td>'
                f'<td style="{cell}">{html_module.escape(item.platform or "")}</td>'
                f'<td style="{cell}">{_item_status_pill(item)}</td>'
                f'<td style="{cell}white-space:nowrap;">{due}</td>'
                f'<td style="{cell}">{html_module.escape(defect)}</td>'
                '</tr>')
        table = (f'<table style="border-collapse:collapse;width:100%;margin:4px 0 16px;">'
                 f'<thead><tr><th style="{head}">Item</th><th style="{head}">Test Case</th>'
                 f'<th style="{head}">Platform</th><th style="{head}">Status</th>'
                 f'<th style="{head}">Due</th><th style="{head}">Defect</th>'
                 f'</tr></thead><tbody>{body}</tbody></table>')
        groups_html += (
            f'<p style="{heading}">{html_module.escape(tester_name)} '
            f'<span style="color:#6b778c;font-weight:normal;font-size:13px;">'
            f'({len(group)})</span></p>' + table)

    content = (
        '<div style="font-size:20px;font-weight:700;">Test cases by tester</div>'
        f'<div style="margin:2px 0 4px;color:#6b778c;font-size:13px;">'
        f'As at {as_of.strftime("%A %d %B %Y")} &middot; {total} test cases across '
        f'{len(by_tester)} testers</div>'
        + groups_html +
        '<p style="color:#6b778c;font-size:12px;margin-top:16px;">Every tester item, grouped by '
        'tester. Generated from Jira.</p>'
    )
    return _email_card(content)


def generate_tester_breakdown_email(items: List[uat_jira.TesterItem], output_directory: Path,
                                    recipients: str, subject: Optional[str] = None,
                                    as_of: Optional[date] = None) -> Path:
    """Write the by-tester breakdown as a .eml draft ready to download. Nothing is sent."""
    as_of = as_of or date.today()
    return email_draft.write_draft(
        output_path=output_directory / 'test_cases_by_tester.eml',
        subject=subject or f'Test cases by tester - {as_of.strftime("%d %b %Y")}',
        html_body=render_tester_breakdown_email_html(items, as_of),
        recipients=recipients,
    )


def write_tester_breakdown_csv(items: List[uat_jira.TesterItem], output_path: Path) -> int:
    """Write every test case, grouped by tester, to a CSV - the breakdown email as data.

    Returns the number of rows written.
    """
    rows = sorted((item for item in items if item.tester_name),
                  key=lambda item: (item.tester_name, item.effective_due_date or '9999-99-99',
                                    build_display_title(item)))
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['Tester', 'Item', 'Test Case', 'Platform', 'Instrument', 'Action',
                         'Status', 'Start Date', 'Due Date', 'Defect', 'Link'])
        for item in rows:
            writer.writerow([
                item.tester_name,
                item.key,
                _clean_jira_text(build_display_title(item)),
                item.platform,
                item.instrument,
                item.action,
                item.status,
                item.effective_start_date,
                item.effective_due_date,
                ', '.join(item.covering_bug_keys),
                item.url,
            ])
    return len(rows)


def generate_tester_emails(
    items: Iterable[uat_jira.TesterItem],
    tester_email_by_name: Dict[str, str],
    output_directory: Path,
    testers_wanted: Optional[Iterable[str]] = None,
    start_of_range: Optional[date] = None,
    end_of_range: Optional[date] = None,
    subject: Optional[str] = None,
) -> List[TesterEmail]:
    """Write one .eml draft per tester, each ready to download and send."""
    # Stamp the subject with the day it was generated, so a tester can tell one round of
    # emails from the next at a glance.
    subject = subject or f'UAT test cases to complete - {date.today().strftime("%d %b %Y")}'
    items_by_tester = group_items_by_tester(items, testers_wanted, start_of_range, end_of_range)

    attachments = TESTER_EMAIL_ATTACHMENTS
    # Status screenshots referenced by the instructions block, embedded so they render in
    # the body rather than hanging off the message as loose attachments.
    inline_images = [(content_id, STATUS_IMAGE_DIRECTORY / filename)
                     for content_id, filename, _caption in STATUS_IMAGES]

    generated: List[TesterEmail] = []
    for tester_name, tester_items in sorted(items_by_tester.items()):
        if not tester_items:
            continue
        tester_email_address = tester_email_by_name.get(tester_name, '')
        output_path = email_draft.write_draft(
            output_path=output_directory / f'{safe_filename_stem(tester_name)}.eml',
            subject=subject,
            html_body=render_email_html(tester_name, tester_items),
            recipients=tester_email_address,
            attachments=attachments,
            inline_images=inline_images,
        )
        generated.append(TesterEmail(tester_name, tester_email_address,
                                     tester_items, output_path))

    return generated
