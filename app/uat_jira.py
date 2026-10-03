"""Data layer for the UAT tooling.

The source of truth is the Jira API: every issue of the tester issue type in the project
named in config/project.json. Each of those carries its test name, platform and tester in
its summary, read right to left, e.g.

    Bond Options+Manual Capture-Order Gateway-JaneSmith
    Verify remote session and API gateway health-Infrastructure-SamLee

The CSV files that used to drive this are not used any more: they describe the parent
scaffolding (Task and Subtask issues), not the items testers actually work from.
"""

import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import requests

import project
import settings

TESTERS_FILE = settings.TESTERS_FILE

PROJECT = project.current()

START_DATE_FIELD = PROJECT.start_date_field

TESTER_ISSUE_JQL = (f'project = "{PROJECT.jira_project}" '
                    f'AND issuetype = "{PROJECT.tester_issue_type}" ORDER BY key')

ISSUE_FIELDS = [
    'summary', 'status', 'assignee', 'reporter', 'priority', 'created', 'updated',
    'description', 'comment', 'attachment', 'duedate', START_DATE_FIELD, 'parent',
    'issuelinks',
]

# Every Tester issue relates to exactly one Task or Subtask. That linked item is where the
# schedule and the written test case live, so it is the real source of the UAT plan:
#     Epic (grouping) > Task / Subtask (test case + dates) > Tester issues (one per person)
SOURCE_ISSUE_TYPES = set(PROJECT.source_issue_types)
BULK_FETCH_BATCH_SIZE = 100

# Blocked work must carry something explaining why - either a Bug or an Action Item.
# The two are treated identically for coverage; both live under the defect epics and link
# with the configured link type (the defect blocks the test).
BUG_ISSUE_TYPE = PROJECT.bug_issue_type
ACTION_ITEM_ISSUE_TYPE = PROJECT.action_item_issue_type
DEFECT_ISSUE_TYPES = {BUG_ISSUE_TYPE, ACTION_ITEM_ISSUE_TYPE}
BUG_LINK_TYPE = PROJECT.defect_link_type

# Defects raised for this round live under the defect epics. Bugs elsewhere in the project
# belong to earlier work and must not appear in reporting. The defects epic is where new
# Bugs are created; the readiness epic holds Action Items for things that are not broken,
# just not ready. Both are scanned, since the summary lists them together.
UAT_DEFECT_EPIC_KEY = PROJECT.defects_epic

# Development defects raised during the round; the Dev Defects tab lists them.
DEV_DEFECTS_EPIC_KEY = PROJECT.dev_defects_epic

# Every epic a live defect can sit under. The dev defects epic is included because a defect
# moved there still explains why its test cannot run - leaving it out made those tests
# report as failures with no defect, which is how real blockers went unseen. Unset epics are
# simply skipped.
UAT_DEFECT_EPIC_KEYS = tuple(key for key in (UAT_DEFECT_EPIC_KEY, PROJECT.readiness_epic,
                                             DEV_DEFECTS_EPIC_KEY) if key)

# Compliance rules are the Tasks under this epic, less the statuses that mean out of scope.
COMPLIANCE_EPIC_KEY = PROJECT.compliance_epic
COMPLIANCE_EXCLUDED_STATUSES = {status.lower() for status in PROJECT.compliance_excluded_statuses}

# Workflow status names, lower-cased once for comparison.
BLOCKED_STATUS = PROJECT.blocked_status.lower()
FAILED_STATUS = PROJECT.failed_status.lower()
RETEST_STATUS = PROJECT.retest_status.lower()
IN_PROGRESS_STATUS = PROJECT.in_progress_status.lower()
TO_DO_STATUS = PROJECT.to_do_status.lower()
DONE_STATUS = PROJECT.done_status.lower()
CANCELLED_STATUSES = {status.lower() for status in PROJECT.cancelled_statuses}

SEARCH_PAGE_SIZE = 100
REQUEST_TIMEOUT_SECONDS = 60

# Statuses whose items should never appear in a tester's to-do email: work that is parked
# or has already failed.
EMAIL_EXCLUDED_STATUSES = {BLOCKED_STATUS, FAILED_STATUS}

# Anything Jira itself considers finished is excluded too. Matching on the status category
# rather than a list of names covers Done, Closed, DUPE and WONT DO, and keeps working if
# someone adds another completed status later.
FINISHED_STATUS_CATEGORY = 'done'

MEDIA_MARKER = '\x00'
MEDIA_MARKER_PATTERN = re.compile(f'{MEDIA_MARKER}(.*?){MEDIA_MARKER}')

CANCEL_ACTION_PATTERN = re.compile(r'\+\s*cancel\b', re.IGNORECASE)


@dataclass
class JiraComment:
    author: str
    created: str
    body: str


@dataclass
class JiraAttachment:
    attachment_id: str
    filename: str
    mime_type: str
    size_bytes: int
    created: str
    author: str
    content_url: str

    @property
    def is_image(self) -> bool:
        return self.mime_type.startswith('image/')


@dataclass
class TesterItem:
    """One 'Tester' issue: a single test case allocated to a single person."""

    key: str
    url: str
    summary: str
    test_name: str
    platform: str
    tester_name: str
    instrument: str
    action: str
    status: str
    status_category: str
    assignee: str
    start_date: str
    due_date: str
    created: str
    updated: str
    description: str
    parent_key: str
    parent_summary: str
    linked_bug_keys: List[str] = field(default_factory=list)
    linked_bug_only_keys: List[str] = field(default_factory=list)
    covering_bug_keys: List[str] = field(default_factory=list)
    covering_bug_summaries: List[str] = field(default_factory=list)
    source_key: str = ''
    source_summary: str = ''
    source_status: str = ''
    source_start_date: str = ''
    source_due_date: str = ''
    test_case_detail: str = ''
    comments: List[JiraComment] = field(default_factory=list)
    attachments: List[JiraAttachment] = field(default_factory=list)

    @property
    def latest_comment(self) -> Optional[JiraComment]:
        return self.comments[-1] if self.comments else None

    @property
    def effective_start_date(self) -> str:
        """A date set directly on the tester item wins; otherwise use the linked schedule."""
        return self.start_date or self.source_start_date

    @property
    def effective_due_date(self) -> str:
        return self.due_date or self.source_due_date

    @property
    def is_blocked(self) -> bool:
        return self.status.strip().lower() == BLOCKED_STATUS

    @property
    def needs_bug(self) -> bool:
        """Blocked work must point at an open defect - a Bug or an Action Item - raised for
        this UAT round.

        A link to a closed one, or to something outside the defect epics, does not explain
        why the test cannot run today, so it does not count as covered.
        """
        return self.is_blocked and not self.covering_bug_keys

    @property
    def has_stale_bug_link(self) -> bool:
        """Linked to a defect, but not one that counts - surface it rather than hide it."""
        return self.is_blocked and bool(self.linked_bug_keys) and not self.covering_bug_keys

    @property
    def is_cancel_test(self) -> bool:
        """The action comes from the linked Subtask, so this is an exact check."""
        return self.action.strip().lower() == 'cancel'

    @property
    def is_email_eligible(self) -> bool:
        """Should this appear in a tester's 'please complete these' email?

        No if it is parked or failed, and no if Jira considers it finished - asking someone
        to run a test they have already completed is worse than leaving it out.
        """
        if self.status.strip().lower() in EMAIL_EXCLUDED_STATUSES:
            return False
        if self.status_category.strip().lower() == FINISHED_STATUS_CATEGORY:
            return False
        return True


def resolve_jira_site() -> str:
    """The Jira site every user shares. Only the credentials differ per person."""
    site = os.environ.get('JIRA_SITE', '').strip()
    if not site:
        raise ValueError('Missing JIRA_SITE. Copy .env.example to .env and fill it in.')
    return site.rstrip('/')


def resolve_jira_configuration(site: Optional[str] = None, email: Optional[str] = None,
                               token: Optional[str] = None) -> Dict[str, str]:
    """Fall back to the single set of credentials in the environment.

    Used for start-up checks and for any code path with no signed-in user; normal request
    handling passes the signed-in person's own email and token instead.
    """
    site = site or os.environ.get('JIRA_SITE')
    email = email or os.environ.get('JIRA_USER_EMAIL')
    token = token or os.environ.get('JIRA_API_TOKEN')

    missing = [name for name, value in
               [('JIRA_SITE', site), ('JIRA_USER_EMAIL', email), ('JIRA_API_TOKEN', token)]
               if not value]
    if missing:
        raise ValueError(
            f'Missing Jira configuration: {", ".join(missing)}. '
            f'Copy .env.example to .env and fill it in.'
        )
    return {'site': site.rstrip('/'), 'email': email, 'token': token}


def build_session(configuration: Optional[Dict[str, str]] = None) -> requests.Session:
    """An authenticated Jira session, tagged with the site so callers need not re-resolve it."""
    configuration = configuration or resolve_jira_configuration()
    session = requests.Session()
    session.auth = (configuration['email'], configuration['token'])
    session.headers.update({'Accept': 'application/json'})
    session.jira_site = configuration['site']
    return session


def build_session_for_credentials(email: str, token: str) -> requests.Session:
    """A session that writes to Jira as the given person.

    Every action the UI takes goes out under the signed-in user's own API token, so the
    Jira history shows who actually made each change.
    """
    if not email or not token:
        raise ValueError('This user has no Jira email or API token configured.')
    return build_session({'site': resolve_jira_site(), 'email': email, 'token': token})


def load_tester_directory() -> List[Dict[str, object]]:
    if not TESTERS_FILE.is_file():
        return []
    return json.loads(TESTERS_FILE.read_text(encoding='utf-8')).get('testers', [])


def build_compact_name_lookup(tester_directory: Iterable[Dict[str, object]]) -> Dict[str, str]:
    """Map 'janesmith' -> 'Jane Smith' so titles can be matched to people."""
    return {str(tester['name']).replace(' ', '').lower(): str(tester['name'])
            for tester in tester_directory}


def parse_tester_summary(summary: str, compact_name_lookup: Dict[str, str]) -> Dict[str, str]:
    """Split a Tester issue summary into its parts, reading right to left.

    The trailing two hyphen-separated segments are always the platform and the tester.
    Splitting from the right avoids tripping over hyphens inside the test name itself,
    such as 'Step-Out' or 'non-Ack by T+1'.
    """
    segments = summary.rsplit('-', 2)
    if len(segments) == 3:
        test_name, platform, trailing_name = (part.strip() for part in segments)
    else:
        test_name, platform, trailing_name = summary.strip(), '', ''

    tester_name = compact_name_lookup.get(trailing_name.replace(' ', '').lower(), '')
    if not tester_name:
        # Unrecognised trailing segment: treat the whole summary as the test name so the
        # item is still visible rather than silently mis-attributed.
        return {'test_name': summary.strip(), 'platform': '', 'tester_name': '',
                'instrument': '', 'action': ''}

    # Instrument and action are deliberately NOT parsed out of the title here. Titles
    # contain incidental plus signs ('non-Ack by T+1 EOD', 'Vendor A + Vendor B'), so
    # splitting on '+' invents nonsense values. They are filled in from the linked source
    # work item instead, which holds them as real fields - see attach_source_details.
    return {'test_name': test_name, 'platform': platform, 'tester_name': tester_name,
            'instrument': '', 'action': ''}


def render_rich_text(node: object) -> str:
    """Flatten an Atlassian Document Format node into readable plain text."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return ''.join(render_rich_text(child) for child in node)
    if not isinstance(node, dict):
        return ''

    node_type = node.get('type')
    if node_type == 'text':
        return node.get('text', '')
    if node_type == 'hardBreak':
        return '\n'
    if node_type == 'mention':
        return '@' + node.get('attrs', {}).get('text', '').lstrip('@')
    if node_type == 'media':
        # Media ids are media-services UUIDs, but 'alt' holds the attachment filename,
        # so mark it and let the presentation layer turn it into a link.
        return f"{MEDIA_MARKER}{node.get('attrs', {}).get('alt', '')}{MEDIA_MARKER}"

    rendered_children = render_rich_text(node.get('content', []))
    if node_type in {'paragraph', 'heading', 'listItem', 'blockquote', 'codeBlock'}:
        return rendered_children + '\n'
    return rendered_children


def build_comment(raw_comment: Dict) -> JiraComment:
    return JiraComment(
        author=raw_comment.get('author', {}).get('displayName', ''),
        created=raw_comment.get('created', ''),
        body=render_rich_text(raw_comment.get('body', '')).strip(),
    )


def build_attachment(raw_attachment: Dict) -> JiraAttachment:
    return JiraAttachment(
        attachment_id=str(raw_attachment.get('id', '')),
        filename=raw_attachment.get('filename', ''),
        mime_type=raw_attachment.get('mimeType', ''),
        size_bytes=raw_attachment.get('size', 0),
        created=raw_attachment.get('created', ''),
        author=raw_attachment.get('author', {}).get('displayName', ''),
        content_url=raw_attachment.get('content', ''),
    )


def find_linked_source_key(fields: Dict) -> str:
    """Return the key of the Task or Subtask this tester item was generated from."""
    for link in fields.get('issuelinks') or []:
        other_issue = link.get('outwardIssue') or link.get('inwardIssue') or {}
        other_type = (other_issue.get('fields') or {}).get('issuetype', {}).get('name', '')
        if other_type in SOURCE_ISSUE_TYPES:
            return other_issue.get('key', '')
    return ''


def find_linked_bug_keys(fields: Dict) -> List[str]:
    """Keys of any defect (Bug or Action Item) linked to this item, either direction."""
    defect_keys = []
    for link in fields.get('issuelinks') or []:
        other_issue = link.get('outwardIssue') or link.get('inwardIssue') or {}
        other_type = (other_issue.get('fields') or {}).get('issuetype', {}).get('name', '')
        if other_type in DEFECT_ISSUE_TYPES and other_issue.get('key'):
            defect_keys.append(other_issue['key'])
    return defect_keys


def find_linked_bug_only_keys(fields: Dict) -> List[str]:
    """Keys of linked issues of type Bug specifically, any epic, any direction.

    Unlike covering_bug_keys this does not require the Bug to sit under the UAT Defect epics
    or be open - a Bug raised anywhere (e.g. the Dev DEFECT epic) still counts as a linked bug.
    """
    bug_keys = []
    for link in fields.get('issuelinks') or []:
        other_issue = link.get('outwardIssue') or link.get('inwardIssue') or {}
        other_type = (other_issue.get('fields') or {}).get('issuetype', {}).get('name', '')
        if other_type == BUG_ISSUE_TYPE and other_issue.get('key'):
            bug_keys.append(other_issue['key'])
    return bug_keys


def attach_source_details(session: requests.Session, items: List[TesterItem]) -> None:
    """Copy the schedule and written test case down from each item's linked work item."""
    source_keys = sorted({item.source_key for item in items if item.source_key})
    if not source_keys:
        return

    details_by_key: Dict[str, Dict[str, str]] = {}
    for start_index in range(0, len(source_keys), BULK_FETCH_BATCH_SIZE):
        batch = source_keys[start_index:start_index + BULK_FETCH_BATCH_SIZE]
        response = session.post(
            f'{session.jira_site}/rest/api/3/issue/bulkfetch',
            json={'issueIdsOrKeys': batch,
                  'fields': ['summary', 'duedate', START_DATE_FIELD, 'description',
                             'issuetype', 'parent', 'status']},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        for raw_issue in response.json().get('issues', []):
            source_fields = raw_issue.get('fields', {}) or {}
            source_parent = source_fields.get('parent') or {}
            details_by_key[raw_issue['key']] = {
                'summary': source_fields.get('summary', ''),
                'status': (source_fields.get('status') or {}).get('name', ''),
                # Jira flags sub-task types itself, so this holds whatever they are named.
                'is_subtask': bool(source_fields.get('issuetype', {}).get('subtask')),
                'parent_summary': (source_parent.get('fields') or {}).get('summary', ''),
                'start': source_fields.get(START_DATE_FIELD) or '',
                'due': source_fields.get('duedate') or '',
                'detail': render_rich_text(source_fields.get('description') or '').strip(),
            }

    for item in items:
        details = details_by_key.get(item.source_key)
        if not details:
            continue
        item.source_summary = details['summary']
        item.source_status = details['status']
        item.source_start_date = details['start']
        item.source_due_date = details['due']
        item.test_case_detail = details['detail']

        # A Subtask source names the action it tests ('Cancel', 'Open'), and its own parent
        # Task names the instrument ('ETD Options'). A Task source is a standalone test
        # case with no action of its own, so the action is left blank rather than invented.
        # A Task source's own parent is the Epic (the grouping), not an instrument, so
        # instrument stays blank there rather than being filled with the Epic name.
        if details['is_subtask']:
            item.action = details['summary']
            item.instrument = details['parent_summary']


def build_tester_item(site: str, raw_issue: Dict, compact_name_lookup: Dict[str, str]) -> TesterItem:
    fields = raw_issue.get('fields', {}) or {}
    status = fields.get('status') or {}
    parent = fields.get('parent') or {}
    summary = fields.get('summary', '')
    parts = parse_tester_summary(summary, compact_name_lookup)

    return TesterItem(
        key=raw_issue.get('key', ''),
        url=f"{site}/browse/{raw_issue.get('key', '')}",
        summary=summary,
        test_name=parts['test_name'],
        platform=parts['platform'],
        tester_name=parts['tester_name'],
        instrument=parts['instrument'],
        action=parts['action'],
        status=status.get('name', ''),
        status_category=(status.get('statusCategory') or {}).get('key', ''),
        assignee=(fields.get('assignee') or {}).get('displayName', ''),
        start_date=fields.get(START_DATE_FIELD) or '',
        due_date=fields.get('duedate') or '',
        created=fields.get('created', ''),
        updated=fields.get('updated', ''),
        description=render_rich_text(fields.get('description') or '').strip(),
        parent_key=parent.get('key', ''),
        parent_summary=(parent.get('fields') or {}).get('summary', ''),
        source_key=find_linked_source_key(fields),
        linked_bug_keys=find_linked_bug_keys(fields),
        linked_bug_only_keys=find_linked_bug_only_keys(fields),
        comments=[build_comment(raw) for raw in (fields.get('comment') or {}).get('comments', [])],
        attachments=[build_attachment(raw) for raw in (fields.get('attachment') or [])],
    )


def attach_bug_coverage(session: requests.Session, items: List[TesterItem]) -> None:
    """Work out which linked bugs actually count as covering a blocked item.

    A bug counts only if it sits under one of the defect epics and is still open. Anything
    else - a closed defect, or a bug from earlier work - leaves the item uncovered.
    """
    defect_bugs = {bug['key']: bug for bug in fetch_bugs(session)}
    open_defect_keys = {
        key for key, bug in defect_bugs.items()
        if bug['status_category'].strip().lower() != FINISHED_STATUS_CATEGORY
    }

    for item in items:
        covering = [key for key in item.linked_bug_keys if key in open_defect_keys]
        item.covering_bug_keys = covering
        item.covering_bug_summaries = [defect_bugs[key]['summary'] for key in covering]


def fetch_tester_items(session: requests.Session, progress: bool = True) -> List[TesterItem]:
    """Page through every tester issue in the configured project."""
    compact_name_lookup = build_compact_name_lookup(load_tester_directory())
    items: List[TesterItem] = []
    next_page_token: Optional[str] = None

    while True:
        parameters = {
            'jql': TESTER_ISSUE_JQL,
            'maxResults': SEARCH_PAGE_SIZE,
            'fields': ','.join(ISSUE_FIELDS),
        }
        if next_page_token:
            parameters['nextPageToken'] = next_page_token

        response = session.get(f'{session.jira_site}/rest/api/3/search/jql',
                               params=parameters, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()

        for raw_issue in payload.get('issues', []):
            items.append(build_tester_item(session.jira_site, raw_issue, compact_name_lookup))

        if progress:
            print(f'  fetched {len(items)} tester items...')

        next_page_token = payload.get('nextPageToken')
        if not next_page_token or payload.get('isLast'):
            break

    attach_source_details(session, items)
    attach_bug_coverage(session, items)

    unlinked = [item for item in items if not item.source_key]
    if unlinked:
        print(f'WARNING: {len(unlinked)} item(s) have no linked Task or Subtask, so they have '
              f'no schedule or test case, e.g. {unlinked[0].key}', file=sys.stderr)

    unattributed = [item for item in items if not item.tester_name]
    if unattributed:
        print(f'WARNING: {len(unattributed)} item(s) have a title whose trailing name does not '
              f'match a known tester, e.g. {unattributed[0].key}: {unattributed[0].summary!r}',
              file=sys.stderr)

    return items


def fetch_bugs(session: requests.Session, limit: int = 200) -> List[Dict[str, str]]:
    """Defects (Bug or Action Item) under the defect epics, with their dates.

    With no defect epic configured there is nothing to scope the search to, so nothing is
    returned rather than every bug the project has ever had.
    """
    if not UAT_DEFECT_EPIC_KEYS:
        return []
    defect_types = ', '.join(f'"{name}"' for name in sorted(DEFECT_ISSUE_TYPES))
    defect_epics = ', '.join(UAT_DEFECT_EPIC_KEYS)
    response = session.get(
        f'{session.jira_site}/rest/api/3/search/jql',
        params={'jql': f'project = "{PROJECT.jira_project}" AND issuetype in ({defect_types}) '
                       f'AND parent in ({defect_epics}) ORDER BY created DESC',
                'maxResults': limit,
                'fields': 'summary,status,issuetype,created,duedate,description,'
                          f'{START_DATE_FIELD},assignee,parent'},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()

    bugs = []
    for raw_issue in response.json().get('issues', []):
        fields = raw_issue.get('fields', {}) or {}
        status = fields.get('status') or {}
        bugs.append({
            'key': raw_issue['key'],
            'url': f"{session.jira_site}/browse/{raw_issue['key']}",
            'summary': fields.get('summary', ''),
            'description': render_rich_text(fields.get('description') or '').strip(),
            'issueType': (fields.get('issuetype') or {}).get('name', ''),
            'status': status.get('name', ''),
            'status_category': (status.get('statusCategory') or {}).get('key', ''),
            'assignee': (fields.get('assignee') or {}).get('displayName', ''),
            # Bugs often have no explicit start date, so fall back to when they were raised.
            'start_date': fields.get(START_DATE_FIELD) or (fields.get('created') or '')[:10],
            'has_explicit_start': bool(fields.get(START_DATE_FIELD)),
            'due_date': fields.get('duedate') or '',
            'epic': (fields.get('parent') or {}).get('fields', {}).get('summary', ''),
        })
    return bugs


def fetch_compliance_items(session: requests.Session) -> List[Dict[str, object]]:
    """Compliance rules: the Tasks under the compliance epic, less the out-of-scope statuses.

    Returns nothing when no compliance epic is configured.
    """
    items: List[Dict[str, object]] = []
    if not COMPLIANCE_EPIC_KEY:
        return items
    next_page_token: Optional[str] = None

    while True:
        parameters = {
            'jql': f'parent = {COMPLIANCE_EPIC_KEY} ORDER BY key',
            'maxResults': SEARCH_PAGE_SIZE,
            'fields': f'summary,status,assignee,priority,duedate,{START_DATE_FIELD},updated',
        }
        if next_page_token:
            parameters['nextPageToken'] = next_page_token

        response = session.get(f'{session.jira_site}/rest/api/3/search/jql',
                               params=parameters, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()

        for raw_issue in payload.get('issues', []):
            fields = raw_issue.get('fields', {}) or {}
            status = fields.get('status') or {}
            if status.get('name', '').strip().lower() in COMPLIANCE_EXCLUDED_STATUSES:
                continue
            items.append({
                'key': raw_issue['key'],
                'url': f"{session.jira_site}/browse/{raw_issue['key']}",
                'summary': fields.get('summary', ''),
                'status': status.get('name', ''),
                'statusCategory': (status.get('statusCategory') or {}).get('key', ''),
                'assignee': (fields.get('assignee') or {}).get('displayName', '') or 'Unassigned',
                'priority': (fields.get('priority') or {}).get('name', ''),
                'dueDate': fields.get('duedate') or '',
                'startDate': fields.get(START_DATE_FIELD) or '',
                'updated': (fields.get('updated') or '')[:10],
            })

        next_page_token = payload.get('nextPageToken')
        if not next_page_token or payload.get('isLast'):
            break

    return items


def fetch_dev_defect_items(session: requests.Session) -> List[Dict[str, object]]:
    """Development defects: every child of the dev defects epic, all statuses.

    Returns nothing when no dev defects epic is configured.
    """
    items: List[Dict[str, object]] = []
    if not DEV_DEFECTS_EPIC_KEY:
        return items
    next_page_token: Optional[str] = None

    while True:
        parameters = {
            'jql': f'parent = {DEV_DEFECTS_EPIC_KEY} ORDER BY key',
            'maxResults': SEARCH_PAGE_SIZE,
            'fields': 'summary,status,assignee,priority,updated',
        }
        if next_page_token:
            parameters['nextPageToken'] = next_page_token

        response = session.get(f'{session.jira_site}/rest/api/3/search/jql',
                               params=parameters, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        payload = response.json()

        for raw_issue in payload.get('issues', []):
            fields = raw_issue.get('fields', {}) or {}
            status = fields.get('status') or {}
            items.append({
                'key': raw_issue['key'],
                'url': f"{session.jira_site}/browse/{raw_issue['key']}",
                'summary': fields.get('summary', ''),
                'status': status.get('name', ''),
                'statusCategory': (status.get('statusCategory') or {}).get('key', ''),
                'assignee': (fields.get('assignee') or {}).get('displayName', '') or 'Unassigned',
                'priority': (fields.get('priority') or {}).get('name', ''),
                'updated': (fields.get('updated') or '')[:10],
            })

        next_page_token = payload.get('nextPageToken')
        if not next_page_token or payload.get('isLast'):
            break

    return items


def format_jira_timestamp(raw_timestamp: str) -> str:
    if not raw_timestamp:
        return ''
    try:
        parsed = datetime.strptime(raw_timestamp[:19], '%Y-%m-%dT%H:%M:%S')
    except ValueError:
        return raw_timestamp
    return parsed.strftime('%d %b %Y  %H:%M')
