"""The project this instance tracks, read from config/project.json.

Which Jira project, which issue types and epics, what the workflow statuses are called,
when the round ends: all of it describes one organisation's Jira rather than how the app
works, so none of it lives in the code. Copy config/project.example.json to
config/project.json and edit it. Only `jira.project` is required; everything else
defaults to Jira's own naming.

The file is read once, at start-up. A mistake stops the app with a message naming the
key, rather than surfacing later as a JQL error or a list that is silently empty.
"""

import json
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional, Tuple

import settings


class ProjectConfigurationError(RuntimeError):
    """config/project.json is missing or does not make sense."""


@dataclass(frozen=True)
class EmailAttachment:
    """A file in config/attachments/ that is attached to every tester email."""

    file_name: str
    display_name: str


@dataclass(frozen=True)
class Project:
    jira_project: str
    tester_issue_type: str = 'Tester'
    source_issue_types: Tuple[str, ...] = ('Task', 'Subtask')
    start_date_field: str = 'customfield_10015'
    bug_issue_type: str = 'Bug'
    action_item_issue_type: str = 'Action Item'
    defect_link_type: str = 'Blocks'
    # The priority Jira gives every new issue. Exports treat it as "not set" rather than as
    # a real ranking. Blank means every priority is reported as it stands.
    default_priority: str = ''

    defects_epic: str = ''
    readiness_epic: str = ''
    dev_defects_epic: str = ''
    compliance_epic: str = ''

    to_do_status: str = 'To Do'
    in_progress_status: str = 'In Progress'
    done_status: str = 'Done'
    blocked_status: str = 'Blocked'
    failed_status: str = 'Testing Failed'
    retest_status: str = 'Retest'
    cancelled_statuses: Tuple[str, ...] = ("Won't Do", 'Wont Do', 'Dupe')
    compliance_excluded_statuses: Tuple[str, ...] = ("Won't Do", 'Wont Do')

    testing_window_business_days: int = 2
    end_date: Optional[date] = None

    guide_url: str = ''
    defects_raised_by: str = 'the test leads'
    questions_channel: str = 'the project channel'
    attachments: Tuple[EmailAttachment, ...] = ()

    export_excluded_testers: Tuple[str, ...] = ()
    export_date_format: str = '%Y-%m-%d'


# (section, key in the file) -> (field, kind). The kind says how the value is read.
_SCHEMA: Dict[Tuple[str, str], Tuple[str, str]] = {
    ('jira', 'project'): ('jira_project', 'text'),
    ('jira', 'testerIssueType'): ('tester_issue_type', 'text'),
    ('jira', 'sourceIssueTypes'): ('source_issue_types', 'texts'),
    ('jira', 'startDateField'): ('start_date_field', 'text'),
    ('jira', 'bugIssueType'): ('bug_issue_type', 'text'),
    ('jira', 'actionItemIssueType'): ('action_item_issue_type', 'text'),
    ('jira', 'defectLinkType'): ('defect_link_type', 'text'),
    ('jira', 'defaultPriority'): ('default_priority', 'optional text'),
    ('epics', 'defects'): ('defects_epic', 'optional text'),
    ('epics', 'readiness'): ('readiness_epic', 'optional text'),
    ('epics', 'devDefects'): ('dev_defects_epic', 'optional text'),
    ('epics', 'compliance'): ('compliance_epic', 'optional text'),
    ('statuses', 'toDo'): ('to_do_status', 'text'),
    ('statuses', 'inProgress'): ('in_progress_status', 'text'),
    ('statuses', 'done'): ('done_status', 'text'),
    ('statuses', 'blocked'): ('blocked_status', 'text'),
    ('statuses', 'failed'): ('failed_status', 'text'),
    ('statuses', 'retest'): ('retest_status', 'text'),
    ('statuses', 'cancelled'): ('cancelled_statuses', 'texts'),
    ('statuses', 'complianceExcluded'): ('compliance_excluded_statuses', 'texts'),
    ('schedule', 'testingWindowBusinessDays'): ('testing_window_business_days', 'days'),
    ('schedule', 'endDate'): ('end_date', 'date'),
    ('emails', 'guideUrl'): ('guide_url', 'optional text'),
    ('emails', 'defectsRaisedBy'): ('defects_raised_by', 'text'),
    ('emails', 'questionsChannel'): ('questions_channel', 'text'),
    ('emails', 'attachments'): ('attachments', 'attachments'),
    ('export', 'excludedTesters'): ('export_excluded_testers', 'optional texts'),
    ('export', 'dateFormat'): ('export_date_format', 'date format'),
}


def _read_value(where: str, kind: str, value: object) -> object:
    if kind in ('text', 'optional text'):
        if not isinstance(value, str) or (kind == 'text' and not value.strip()):
            raise ProjectConfigurationError(f'{where} must be a non-empty string.'
                                            if kind == 'text' else f'{where} must be a string.')
        return value.strip()
    if kind in ('texts', 'optional texts'):
        if not isinstance(value, list) or not all(isinstance(entry, str) and entry.strip()
                                                  for entry in value):
            raise ProjectConfigurationError(f'{where} must be a list of non-empty strings.')
        if kind == 'texts' and not value:
            raise ProjectConfigurationError(f'{where} must list at least one value.')
        return tuple(entry.strip() for entry in value)
    if kind == 'days':
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ProjectConfigurationError(f'{where} must be a whole number of days, 0 or more.')
        return value
    if kind == 'date':
        if value in (None, ''):
            return None
        try:
            return date.fromisoformat(str(value))
        except ValueError:
            raise ProjectConfigurationError(f'{where} must be a date written YYYY-MM-DD.') from None
    if kind == 'date format':
        if not isinstance(value, str) or '%' not in value:
            raise ProjectConfigurationError(f'{where} must be a strftime format such as "%d/%m/%Y".')
        return value
    if kind == 'attachments':
        if not isinstance(value, list):
            raise ProjectConfigurationError(f'{where} must be a list.')
        attachments = []
        for position, entry in enumerate(value):
            if (not isinstance(entry, dict) or not isinstance(entry.get('file'), str)
                    or not entry['file'].strip()):
                raise ProjectConfigurationError(
                    f'{where}[{position}] must be an object with a "file" and an optional "name".')
            file_name = entry['file'].strip()
            # Attachments are read from config/attachments/ only; a path would reach outside it.
            if Path(file_name).name != file_name:
                raise ProjectConfigurationError(
                    f'{where}[{position}].file must be a file name in config/attachments/, not a path.')
            attachments.append(EmailAttachment(file_name, str(entry.get('name') or file_name).strip()))
        return tuple(attachments)
    raise AssertionError(f'unknown kind {kind}')


def parse_project(document: object) -> Project:
    """Turn the parsed JSON into a Project, refusing anything unexpected by name."""
    if not isinstance(document, dict):
        raise ProjectConfigurationError('project.json must contain a JSON object.')

    values: Dict[str, object] = {}
    unknown = []
    known_sections = {section for section, _key in _SCHEMA}
    for section, content in document.items():
        if section.startswith('_'):
            continue  # "_comment" and friends: notes for whoever edits the file
        if section not in known_sections:
            unknown.append(section)
            continue
        if not isinstance(content, dict):
            raise ProjectConfigurationError(f'"{section}" must be an object.')
        for key, value in content.items():
            if key.startswith('_'):
                continue
            if (section, key) not in _SCHEMA:
                unknown.append(f'{section}.{key}')
                continue
            field_name, kind = _SCHEMA[(section, key)]
            values[field_name] = _read_value(f'{section}.{key}', kind, value)

    if unknown:
        raise ProjectConfigurationError(
            f'project.json has settings this version does not understand: {", ".join(unknown)}. '
            f'Check the spelling against config/project.example.json.')
    if 'jira_project' not in values:
        raise ProjectConfigurationError('project.json must set jira.project, the Jira project key.')
    return Project(**values)


def load_project(path: Path) -> Project:
    if not path.is_file():
        raise ProjectConfigurationError(
            f'No project definition at {path}. Copy config/project.example.json to '
            f'config/project.json and set jira.project to your Jira project key.')
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as decode_error:
        raise ProjectConfigurationError(f'{path.name} is not valid JSON: {decode_error}') from None
    return parse_project(document)


@lru_cache(maxsize=1)
def current() -> Project:
    """The project this instance is configured for, read once and then reused."""
    return load_project(settings.PROJECT_FILE)
