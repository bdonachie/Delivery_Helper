"""The Jira write primitives the web app builds its bulk actions from.

Each function here does one small thing against the Jira REST API. The routes in app.py
compose them into the operations the UI offers - comment, transition, set dates, assign -
and every call is made with the signed-in user's own session, so Jira records who did it.
"""

from datetime import date, timedelta
from typing import Dict, Optional

import requests

import project

START_DATE_FIELD = project.current().start_date_field
DUE_DATE_FIELD = 'duedate'

# Business days allowed between a test's start and its due date (config/project.json).
TESTING_WINDOW_BUSINESS_DAYS = project.current().testing_window_business_days


def add_business_days(start: date, business_days: int) -> date:
    """The date `business_days` working days after `start`, skipping weekends."""
    current_date = start
    remaining_days = business_days
    while remaining_days > 0:
        current_date += timedelta(days=1)
        if current_date.weekday() < 5:
            remaining_days -= 1
    return current_date


def find_transition_id(session: requests.Session, site: str, issue_key: str,
                       target_status_name: str) -> Optional[str]:
    """The id of the transition that lands `issue_key` in the named status, if one exists.

    Jira exposes transitions rather than statuses, and which are available depends on where
    the issue currently sits - so this is resolved per issue rather than cached.
    """
    response = session.get(f'{site}/rest/api/3/issue/{issue_key}/transitions', timeout=30)
    response.raise_for_status()
    for transition in response.json().get('transitions', []):
        if transition.get('to', {}).get('name', '').lower() == target_status_name.lower():
            return transition['id']
    return None


def build_comment_document(comment_text: str) -> Dict:
    """Wrap plain text as an Atlassian Document Format paragraph list.

    The v3 API refuses a plain string body, so even a one-line comment has to be posted as
    a document.
    """
    return {
        'type': 'doc',
        'version': 1,
        'content': [{'type': 'paragraph', 'content': [{'type': 'text', 'text': paragraph}]}
                    for paragraph in comment_text.split('\n') if paragraph.strip()],
    }
