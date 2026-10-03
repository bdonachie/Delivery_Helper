"""Delivery Helper - a web app for running a UAT round whose test cases live in Jira.

Runs as a container on the network rather than on one person's desktop, so:

  * everyone signs in as themselves and every write goes to Jira under their own API
    token (see auth.py), which is what the "Logged in as" control in the header selects;
  * exports and email drafts are written into the mounted data volume and handed back as
    downloads, because the browser is no longer on the same machine as the server;
  * the issue list is kept current by polling - the page asks for changes on a timer, and
    "Refresh now" forces one immediately.

Data comes straight from the Jira API. Nothing is written to Jira until an action is
confirmed in the UI, and no email is ever sent - drafts are downloaded and sent by hand.
"""

import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

import requests
from flask import (Flask, jsonify, render_template, request, send_from_directory,
                   url_for)

import auth
import email_builder
import export_by_tester
import export_uat_status
import jira_actions
import settings
import uat_jira

application = Flask(__name__)
application.secret_key = settings.FLASK_SECRET_KEY

settings.ensure_writable_directories()

# Files the browser is allowed to download, in the order they are searched. Both sit inside
# the data volume; nothing outside them is reachable through /download.
DOWNLOADABLE_DIRECTORIES = (settings.EXPORT_DIRECTORY, settings.EMAIL_OUTPUT_DIRECTORY)

issue_cache: Dict[str, object] = {'items': [], 'loaded_at': None, 'fetched_monotonic': None}

@application.after_request
def prevent_stale_pages(response):
    """Stop the browser caching the app's own HTML.

    The page carries its own JavaScript, so a cached copy keeps running old code
    against a new server. That is how the export buttons came to point at
    Windows file paths after the server had moved to /download URLs: the
    browser was still running the previous page, and a browser silently refuses
    to navigate to a local path, so clicking simply did nothing.

    Generated files are excluded - those are immutable once written and worth
    caching.
    """
    content_type = response.headers.get('Content-Type', '')
    is_page = content_type.startswith('text/html')
    is_attachment = 'attachment' in response.headers.get('Content-Disposition', '')
    if is_page and not is_attachment:
        response.headers['Cache-Control'] = 'no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
    return response


cache_lock = threading.Lock()


@application.context_processor
def inject_application_name():
    """Every page shows the same name, set once in settings."""
    return {'app_name': settings.APPLICATION_NAME}


# --------------------------------------------------------------------------- #
# Jira access
# --------------------------------------------------------------------------- #

def jira_session() -> requests.Session:
    """A Jira session authenticated as whoever is signed in.

    Reads and writes both go out under the signed-in person's own token, so the Jira
    history names the person who actually pressed the button.
    """
    user = auth.current_user()
    if user is None:
        raise ValueError('Not signed in.')
    return uat_jira.build_session_for_credentials(user.email, user.api_token)


def cache_age_seconds() -> Optional[float]:
    fetched_at = issue_cache['fetched_monotonic']
    return None if fetched_at is None else time.monotonic() - fetched_at


def load_items(force_refresh: bool = False) -> List[uat_jira.TesterItem]:
    """The tester items, from cache when recent enough.

    Testers change issues in Jira while this app is open, so a cached copy is only trusted
    for a few seconds; the poll loop and every write path refetch beyond that.
    """
    with cache_lock:
        age = cache_age_seconds()
        cache_is_usable = (issue_cache['items']
                           and age is not None
                           and age < settings.CACHE_MAX_AGE_SECONDS)
        if cache_is_usable and not force_refresh:
            return issue_cache['items']

        items = uat_jira.fetch_tester_items(jira_session(), progress=False)
        issue_cache['items'] = items
        issue_cache['loaded_at'] = datetime.now().strftime('%H:%M:%S')
        issue_cache['fetched_monotonic'] = time.monotonic()
        return items


def invalidate_issue_cache() -> None:
    """Force the next read to go back to Jira."""
    with cache_lock:
        issue_cache['fetched_monotonic'] = None


def item_to_dictionary(item: uat_jira.TesterItem) -> Dict[str, object]:
    latest_comment = item.latest_comment
    return {
        'key': item.key,
        'url': item.url,
        'testName': item.test_name,
        'summary': item.summary,
        'tester': item.tester_name,
        'platform': item.platform,
        'instrument': item.instrument,
        'action': item.action,
        'status': item.status,
        'statusCategory': item.status_category,
        'assignee': item.assignee or 'Unassigned',
        'startDate': item.effective_start_date,
        'dueDate': item.effective_due_date,
        'dateIsOverride': bool(item.start_date),
        'updated': item.updated,
        'parentKey': item.parent_key,
        'parentSummary': item.parent_summary,
        'sourceKey': item.source_key,
        'sourceSummary': item.source_summary,
        'testCaseDetail': item.test_case_detail,
        'linkedBugs': item.linked_bug_keys,
        'coveringBugs': item.covering_bug_keys,
        'coveringBugSummaries': item.covering_bug_summaries,
        'needsBug': item.needs_bug,
        'hasStaleBugLink': item.has_stale_bug_link,
        'isBlocked': item.is_blocked,
        'commentCount': len(item.comments),
        'attachmentCount': len(item.attachments),
        'latestCommentAuthor': latest_comment.author if latest_comment else '',
        'latestCommentDate': latest_comment.created if latest_comment else '',
        'latestCommentBody': latest_comment.body[:400] if latest_comment else '',
        'emailEligible': item.is_email_eligible,
    }


def download_url(file_path) -> str:
    """The URL the browser fetches a generated file from."""
    return url_for('download_generated_file', filename=Path(file_path).name)


# --------------------------------------------------------------------------- #
# Signing in
# --------------------------------------------------------------------------- #

@application.route('/login')
def login_page():
    return render_template('login.html')


@application.route('/api/session')
def api_session():
    """Who is signed in, and who could be. Deliberately open, so the log-in page can ask."""
    user = auth.current_user()
    try:
        users = auth.selectable_users()
        directory_error = ''
    except auth.UserDirectoryError as configuration_error:
        users, directory_error = [], str(configuration_error)
    return jsonify({
        'user': ({'id': user.identifier, 'name': user.name, 'email': user.email}
                 if user else None),
        'users': users,
        'error': directory_error,
    })


@application.route('/api/session', methods=['POST'])
def api_sign_in():
    payload = request.get_json(silent=True) or {}
    try:
        user = auth.find_user(payload.get('userId', ''))
    except auth.UserDirectoryError as configuration_error:
        return jsonify({'error': str(configuration_error)}), 500

    if user is None:
        return jsonify({'error': 'Unknown user.'}), 404
    if not user.has_credentials:
        return jsonify({'error': f'{user.name} has no Jira email or API token configured. '
                                 f'Add one to config/users.json and the .env, then '
                                 f'restart the container.'}), 400
    if user.requires_access_code and \
            (payload.get('accessCode') or '').strip() != user.access_code:
        return jsonify({'error': 'That access code is not right.'}), 403

    auth.sign_in(user)
    # A different person means different credentials, so nothing cached for the previous
    # user should be handed to this one.
    invalidate_issue_cache()
    return jsonify({'user': {'id': user.identifier, 'name': user.name, 'email': user.email}})


@application.route('/api/session', methods=['DELETE'])
def api_sign_out():
    auth.sign_out()
    return jsonify({'signedOut': True})


@application.route('/healthz')
def health_check():
    """Liveness probe for Docker. Answers without touching Jira, so a Jira outage does not
    make the container look dead."""
    return jsonify({'status': 'ok'})


# --------------------------------------------------------------------------- #
# Issues
# --------------------------------------------------------------------------- #

@application.route('/')
@auth.login_required
def index():
    return render_template('index.html')


@application.route('/api/issues')
@auth.login_required
def api_issues():
    try:
        items = load_items(request.args.get('refresh') == 'true')
    except (ValueError, requests.RequestException) as error:
        return jsonify({'error': str(error)}), 500
    age = cache_age_seconds()
    return jsonify({
        'issues': [item_to_dictionary(item) for item in items],
        'testers': uat_jira.load_tester_directory(),
        'loadedAt': issue_cache['loaded_at'],
        'cacheAgeSeconds': round(age, 1) if age is not None else None,
        # Report the statuses actually being excluded right now rather than a fixed label,
        # so the UI stays truthful if the workflow gains a new completed status.
        'excludedStatuses': sorted({item.status for item in items
                                    if not item.is_email_eligible}),
    })


def run_over_issues(issue_keys: List[str], operation: Callable) -> Dict[str, object]:
    """Apply an operation to each key, collecting per-issue outcomes."""
    session = jira_session()
    results = []
    for issue_key in issue_keys:
        try:
            problems = operation(session, issue_key)
        except requests.RequestException as request_error:
            problems = [str(request_error)]
        results.append({'key': issue_key, 'ok': not problems, 'detail': '; '.join(problems)})
    load_items(force_refresh=True)
    return {'results': results}


@application.route('/api/comment', methods=['POST'])
@auth.login_required
def api_comment():
    payload = request.get_json(force=True)
    issue_keys = payload.get('issueKeys', [])
    comment_text = (payload.get('comment') or '').strip()
    if not issue_keys or not comment_text:
        return jsonify({'error': 'Select issues and enter a comment.'}), 400

    document = jira_actions.build_comment_document(comment_text)

    def post_comment(session, issue_key):
        response = session.post(f'{session.jira_site}/rest/api/3/issue/{issue_key}/comment',
                                json={'body': document}, timeout=30)
        return [] if response.status_code < 400 else [
            f'{response.status_code}: {response.text[:150]}']

    return jsonify(run_over_issues(issue_keys, post_comment))


@application.route('/api/status', methods=['POST'])
@auth.login_required
def api_status():
    payload = request.get_json(force=True)
    issue_keys = payload.get('issueKeys', [])
    target_status = (payload.get('status') or '').strip()
    if not issue_keys or not target_status:
        return jsonify({'error': 'Select issues and a target status.'}), 400

    def transition(session, issue_key):
        transition_id = jira_actions.find_transition_id(
            session, session.jira_site, issue_key, target_status)
        if transition_id is None:
            return [f'no transition to {target_status}']
        response = session.post(f'{session.jira_site}/rest/api/3/issue/{issue_key}/transitions',
                                json={'transition': {'id': transition_id}}, timeout=30)
        return [] if response.status_code < 400 else [
            f'{response.status_code}: {response.text[:150]}']

    return jsonify(run_over_issues(issue_keys, transition))


@application.route('/api/dates', methods=['POST'])
@auth.login_required
def api_dates():
    payload = request.get_json(force=True)
    issue_keys = payload.get('issueKeys', [])
    start_date = (payload.get('startDate') or '').strip()
    if not issue_keys or not start_date:
        return jsonify({'error': 'Select issues and a start date.'}), 400

    parsed_start = email_builder.parse_flexible_date(start_date)
    if parsed_start is None:
        return jsonify({'error': f'Could not read the date {start_date!r}.'}), 400
    derived_due = jira_actions.add_business_days(
        parsed_start, jira_actions.TESTING_WINDOW_BUSINESS_DAYS)

    def set_dates(session, issue_key):
        response = session.put(
            f'{session.jira_site}/rest/api/3/issue/{issue_key}',
            json={'fields': {jira_actions.START_DATE_FIELD: parsed_start.isoformat(),
                             jira_actions.DUE_DATE_FIELD: derived_due.isoformat()}},
            timeout=30)
        return [] if response.status_code < 400 else [
            f'{response.status_code}: {response.text[:150]}']

    result = run_over_issues(issue_keys, set_dates)
    result['derivedDueDate'] = derived_due.isoformat()
    return jsonify(result)


@application.route('/api/assign', methods=['POST'])
@auth.login_required
def api_assign():
    """Assign each item to the tester named in its own title."""
    payload = request.get_json(force=True)
    issue_keys = payload.get('issueKeys', [])
    if not issue_keys:
        return jsonify({'error': 'No issues selected.'}), 400

    tester_directory = uat_jira.load_tester_directory()
    account_id_by_name = {str(tester['name']): str(tester['accountId'])
                          for tester in tester_directory}
    confirmed_names = {str(tester['name']) for tester in tester_directory
                       if tester.get('confirmed')}
    item_by_key = {item.key: item for item in load_items()}

    def assign(session, issue_key):
        item = item_by_key.get(issue_key)
        if item is None:
            return ['issue not in cache']
        if not item.tester_name:
            return ['no tester in title']
        if item.tester_name not in account_id_by_name:
            return [f'no Jira account for {item.tester_name}']
        if item.tester_name not in confirmed_names:
            return [f'{item.tester_name} not confirmed in testers.json']
        response = session.put(
            f'{session.jira_site}/rest/api/3/issue/{issue_key}',
            json={'fields': {'assignee': {'accountId': account_id_by_name[item.tester_name]}}},
            timeout=30)
        return [] if response.status_code < 400 else [
            f'{response.status_code}: {response.text[:150]}']

    return jsonify(run_over_issues(issue_keys, assign))


@application.route('/api/bulk', methods=['POST'])
@auth.login_required
def api_bulk():
    """Apply a comment and/or a status transition to a filtered set of items.

    'statusFrom' is a guard: an item is only transitioned if it is currently in that
    status, so a stale browser selection cannot drag unrelated items along with it.
    """
    payload = request.get_json(force=True)
    issue_keys = payload.get('issueKeys', [])
    comment_text = (payload.get('comment') or '').strip()
    status_from = (payload.get('statusFrom') or '').strip()
    target_status = (payload.get('statusTo') or '').strip()

    if not issue_keys:
        return jsonify({'error': 'Nothing matched the filters.'}), 400
    if not comment_text and not target_status:
        return jsonify({'error': 'Enter a comment, choose a target status, or both.'}), 400

    item_by_key = {item.key: item for item in load_items()}
    comment_document = (jira_actions.build_comment_document(comment_text)
                        if comment_text else None)

    def apply_bulk(session, issue_key):
        problems = []
        item = item_by_key.get(issue_key)

        if comment_document is not None:
            response = session.post(
                f'{session.jira_site}/rest/api/3/issue/{issue_key}/comment',
                json={'body': comment_document}, timeout=30)
            if response.status_code >= 400:
                problems.append(f'comment failed ({response.status_code})')

        if target_status:
            if status_from and item is not None and item.status != status_from:
                problems.append(f'skipped: status is {item.status!r}, not {status_from!r}')
            else:
                transition_id = jira_actions.find_transition_id(
                    session, session.jira_site, issue_key, target_status)
                if transition_id is None:
                    problems.append(f'no transition to {target_status}')
                else:
                    response = session.post(
                        f'{session.jira_site}/rest/api/3/issue/{issue_key}/transitions',
                        json={'transition': {'id': transition_id}}, timeout=30)
                    if response.status_code >= 400:
                        problems.append(f'transition failed ({response.status_code})')
        return problems

    return jsonify(run_over_issues(issue_keys, apply_bulk))


# --------------------------------------------------------------------------- #
# Defects
# --------------------------------------------------------------------------- #

@application.route('/api/bugs')
@auth.login_required
def api_bugs():
    """Open defects under the UAT Defects epic - the only bugs that count as coverage."""
    try:
        bugs = uat_jira.fetch_bugs(jira_session())
    except (ValueError, requests.RequestException) as error:
        return jsonify({'error': str(error)}), 500

    open_bugs = [bug for bug in bugs
                 if bug['status_category'].strip().lower()
                 != uat_jira.FINISHED_STATUS_CATEGORY]
    return jsonify({'bugs': open_bugs,
                    'epicKey': uat_jira.UAT_DEFECT_EPIC_KEY,
                    'closedCount': len(bugs) - len(open_bugs)})


@application.route('/api/bugs/create', methods=['POST'])
@auth.login_required
def api_create_bug():
    payload = request.get_json(force=True)
    summary = (payload.get('summary') or '').strip()
    description = (payload.get('description') or '').strip()
    issue_type = (payload.get('issueType') or uat_jira.BUG_ISSUE_TYPE).strip()
    if not summary:
        return jsonify({'error': 'Enter a summary.'}), 400
    if issue_type not in uat_jira.DEFECT_ISSUE_TYPES:
        issue_type = uat_jira.BUG_ISSUE_TYPE

    session = jira_session()
    fields = {
        'project': {'key': uat_jira.PROJECT.jira_project},
        'issuetype': {'name': issue_type},
        'summary': summary,
    }
    if uat_jira.UAT_DEFECT_EPIC_KEY:
        # Without this the new bug lands outside the defects epic and would not count as
        # covering anything, so the item would still show as needing a defect.
        fields['parent'] = {'key': uat_jira.UAT_DEFECT_EPIC_KEY}
    if description:
        fields['description'] = jira_actions.build_comment_document(description)

    response = session.post(f'{session.jira_site}/rest/api/3/issue',
                            json={'fields': fields}, timeout=30)
    if response.status_code >= 400:
        return jsonify({'error': f'{response.status_code}: {response.text[:300]}'}), 500
    created = response.json()
    return jsonify({'key': created['key'],
                    'url': f'{session.jira_site}/browse/{created["key"]}'})


@application.route('/api/bugs/link', methods=['POST'])
@auth.login_required
def api_link_bug():
    """Link a Bug to each selected item, so blocked work says why it is blocked."""
    payload = request.get_json(force=True)
    issue_keys = payload.get('issueKeys', [])
    bug_key = (payload.get('bugKey') or '').strip().upper()
    if not issue_keys or not bug_key:
        return jsonify({'error': 'Select items and a bug.'}), 400

    item_by_key = {item.key: item for item in load_items()}

    def link_bug(session, issue_key):
        item = item_by_key.get(issue_key)
        if item is not None and bug_key in item.linked_bug_keys:
            return [f'skipped: {bug_key} already linked']
        response = session.post(
            f'{session.jira_site}/rest/api/3/issueLink',
            json={'type': {'name': uat_jira.BUG_LINK_TYPE},
                  # The bug blocks the test, so the bug is the outward side.
                  'outwardIssue': {'key': bug_key},
                  'inwardIssue': {'key': issue_key}},
            timeout=30)
        return [] if response.status_code < 400 else [
            f'{response.status_code}: {response.text[:150]}']

    return jsonify(run_over_issues(issue_keys, link_bug))


# --------------------------------------------------------------------------- #
# Compliance and dev defects
# --------------------------------------------------------------------------- #

compliance_cache: Dict[str, object] = {'items': [], 'fetched_monotonic': None}
dev_defects_cache: Dict[str, object] = {'items': [], 'fetched_monotonic': None}


def cached_fetch(cache: Dict[str, object], fetch: Callable, force_refresh: bool):
    """Serve a secondary list from cache, refetching once it goes stale."""
    with cache_lock:
        fetched_at = cache['fetched_monotonic']
        age = None if fetched_at is None else time.monotonic() - fetched_at
        is_fresh = cache['items'] and age is not None and age < settings.CACHE_MAX_AGE_SECONDS
        if not is_fresh or force_refresh:
            cache['items'] = fetch()
            cache['fetched_monotonic'] = time.monotonic()
        return cache['items']


@application.route('/api/compliance')
@auth.login_required
def api_compliance():
    """Compliance rules from the Compliance epic (Won't Do excluded)."""
    try:
        items = cached_fetch(compliance_cache,
                             lambda: uat_jira.fetch_compliance_items(jira_session()),
                             request.args.get('refresh') == 'true')
    except (ValueError, requests.RequestException) as error:
        return jsonify({'error': str(error)}), 500

    return jsonify({'items': items, 'epicKey': uat_jira.COMPLIANCE_EPIC_KEY,
                    'excludedStatuses': sorted(uat_jira.COMPLIANCE_EXCLUDED_STATUSES)})


@application.route('/api/compliance/email', methods=['POST'])
@auth.login_required
def api_compliance_email():
    """Build a compliance summary draft listing every compliance rule, grouped by status."""
    payload = request.get_json(force=True, silent=True) or {}
    try:
        items = uat_jira.fetch_compliance_items(jira_session())
        output_path = email_builder.generate_compliance_email(
            items, settings.EMAIL_OUTPUT_DIRECTORY, payload.get('recipients', ''))
    except (ValueError, OSError, requests.RequestException) as error:
        return jsonify({'error': f'{type(error).__name__}: {error}'}), 500

    return jsonify({'downloadUrl': download_url(output_path),
                    'fileName': output_path.name, 'count': len(items)})


@application.route('/api/dev-defects')
@auth.login_required
def api_dev_defects():
    """Development defects from the Dev Defects epic (all statuses)."""
    try:
        items = cached_fetch(dev_defects_cache,
                             lambda: uat_jira.fetch_dev_defect_items(jira_session()),
                             request.args.get('refresh') == 'true')
    except (ValueError, requests.RequestException) as error:
        return jsonify({'error': str(error)}), 500

    return jsonify({'items': items, 'epicKey': uat_jira.DEV_DEFECTS_EPIC_KEY})


# --------------------------------------------------------------------------- #
# Exports and downloads
# --------------------------------------------------------------------------- #

@application.route('/api/export', methods=['POST'])
@auth.login_required
def api_export():
    """Build the per-tester CSV straight from Jira: one row per tester per test case."""
    try:
        # Always pull fresh - an export is an explicit action where being current matters
        # more than the few seconds saved by the cache.
        session = jira_session()
        items = load_items(force_refresh=True)
        tester_export = export_by_tester.generate(items=items, session=session)
    except (ValueError, OSError, requests.RequestException) as error:
        return jsonify({'error': f'{type(error).__name__}: {error}'}), 500

    export_path = Path(tester_export['path'])
    blocked_count = sum(1 for item in items
                        if export_uat_status.status_bucket(item) == 'Blocked')
    defect_keys = {key for item in items
                   for key in (item.covering_bug_keys + item.linked_bug_only_keys)}

    # The browser is not on the server any more, so hand back a URL rather than a file path.
    return jsonify({'downloadUrl': download_url(export_path),
                    'fileName': export_path.name,
                    'asOf': date.today().isoformat(),
                    'testCaseCount': tester_export['uatCaseCount'],
                    'rowCount': tester_export['rowCount'],
                    'testerCount': tester_export['testerCount'],
                    'itemCount': len(items),
                    'blockedCount': blocked_count,
                    'defectCount': len(defect_keys)})


@application.route('/download/<path:filename>')
@auth.login_required
def download_generated_file(filename: str):
    """Serve a generated export or email draft. Restricted to the data directories."""
    safe_name = Path(filename).name
    for directory in DOWNLOADABLE_DIRECTORIES:
        if (directory / safe_name).is_file():
            return send_from_directory(directory, safe_name, as_attachment=True)
    return jsonify({'error': f'{safe_name} not found.'}), 404


@application.route('/report/<path:filename>')
@auth.login_required
def view_generated_report(filename: str):
    """Open a generated HTML report in the browser rather than downloading it."""
    safe_name = Path(filename).name
    for directory in DOWNLOADABLE_DIRECTORIES:
        if (directory / safe_name).is_file():
            return send_from_directory(directory, safe_name)
    return jsonify({'error': f'{safe_name} not found.'}), 404


# --------------------------------------------------------------------------- #
# Email drafts
# --------------------------------------------------------------------------- #

@application.route('/api/emails/preview', methods=['POST'])
@auth.login_required
def api_emails_preview():
    payload = request.get_json(force=True)
    try:
        items = load_items(force_refresh=True)
    except (ValueError, requests.RequestException) as error:
        return jsonify({'error': str(error)}), 500

    items_by_tester = email_builder.group_items_by_tester(
        items,
        payload.get('testers') or None,
        email_builder.parse_flexible_date(payload.get('startDate', '')),
        email_builder.parse_flexible_date(payload.get('endDate', '')),
    )
    return jsonify({
        'preview': [{'tester': tester_name, 'count': len(tester_items),
                     'html': email_builder.render_email_html(tester_name, tester_items)}
                    for tester_name, tester_items in sorted(items_by_tester.items())],
        'excludedStatuses': sorted({item.status for item in items
                                    if not item.is_email_eligible}),
    })


@application.route('/api/emails', methods=['POST'])
@auth.login_required
def api_emails():
    payload = request.get_json(force=True)
    tester_email_by_name = {str(tester['name']): str(tester['email'])
                            for tester in uat_jira.load_tester_directory()}
    try:
        generated = email_builder.generate_tester_emails(
            load_items(force_refresh=True),
            tester_email_by_name,
            settings.EMAIL_OUTPUT_DIRECTORY,
            testers_wanted=payload.get('testers') or None,
            start_of_range=email_builder.parse_flexible_date(payload.get('startDate', '')),
            end_of_range=email_builder.parse_flexible_date(payload.get('endDate', '')),
        )
    except (ValueError, OSError, requests.RequestException) as error:
        return jsonify({'error': f'{type(error).__name__}: {error}'}), 500

    return jsonify({
        'emails': [{'tester': draft.tester_name, 'email': draft.tester_email,
                    'count': draft.item_count, 'fileName': draft.output_path.name,
                    'downloadUrl': download_url(draft.output_path)}
                   for draft in generated],
    })


@application.route('/api/summary/preview', methods=['POST'])
@auth.login_required
def api_summary_preview():
    # The status summary is always the whole picture - no date filtering - so the
    # Tests Impacted counts reflect every linked test, not just a slice.
    try:
        session = jira_session()
        items = load_items(force_refresh=True)
        bugs = uat_jira.fetch_bugs(session)
        dev_defect_items = uat_jira.fetch_dev_defect_items(session)
    except (ValueError, requests.RequestException) as error:
        return jsonify({'error': f'{type(error).__name__}: {error}'}), 500

    return jsonify({'html': email_builder.render_summary_email_html(
                        items, None, bugs, dev_defect_items),
                    'itemCount': len(items),
                    'bugCount': len(bugs)})


@application.route('/api/summary', methods=['POST'])
@auth.login_required
def api_summary():
    payload = request.get_json(force=True)
    recipients = (payload.get('recipients') or '').strip()
    if not recipients:
        return jsonify({'error': 'Enter at least one recipient address.'}), 400

    try:
        # Always the whole picture - see the preview endpoint.
        session = jira_session()
        items = load_items(force_refresh=True)
        bugs = uat_jira.fetch_bugs(session)
        dev_defect_items = uat_jira.fetch_dev_defect_items(session)
        output_path = email_builder.generate_summary_email(
            items, settings.EMAIL_OUTPUT_DIRECTORY, recipients,
            bugs=bugs, dev_defect_items=dev_defect_items)
    except (ValueError, OSError, requests.RequestException) as error:
        return jsonify({'error': f'{type(error).__name__}: {error}'}), 500

    return jsonify({'downloadUrl': download_url(output_path),
                    'fileName': output_path.name,
                    'itemCount': len(items), 'bugCount': len(bugs)})


@application.route('/api/emails/breakdown', methods=['POST'])
@auth.login_required
def api_emails_breakdown():
    """Build a draft listing every test case grouped by tester (case, status, due, defect)."""
    try:
        items = load_items(force_refresh=True)
        output_path = email_builder.generate_tester_breakdown_email(
            items, settings.EMAIL_OUTPUT_DIRECTORY, '')
    except (ValueError, OSError, requests.RequestException) as error:
        return jsonify({'error': f'{type(error).__name__}: {error}'}), 500

    tester_count = len({item.tester_name for item in items if item.tester_name})
    return jsonify({'downloadUrl': download_url(output_path), 'fileName': output_path.name,
                    'itemCount': len(items), 'testerCount': tester_count})


@application.route('/api/emails/breakdown.csv', methods=['POST'])
@auth.login_required
def api_emails_breakdown_csv():
    """Write every test case grouped by tester to a CSV (the breakdown email, as data)."""
    output_path = settings.EXPORT_DIRECTORY / 'test_cases_by_tester.csv'
    try:
        items = load_items(force_refresh=True)
        row_count = email_builder.write_tester_breakdown_csv(items, output_path)
    except (ValueError, OSError, requests.RequestException) as error:
        return jsonify({'error': f'{type(error).__name__}: {error}'}), 500

    tester_count = len({item.tester_name for item in items if item.tester_name})
    return jsonify({'downloadUrl': download_url(output_path), 'fileName': output_path.name,
                    'itemCount': row_count, 'testerCount': tester_count})


if __name__ == '__main__':
    # Development entry point only. In the container gunicorn serves `app:application`.
    application.run(host=settings.LISTEN_HOST, port=settings.LISTEN_PORT, debug=False)
