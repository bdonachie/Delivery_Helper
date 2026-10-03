"""Who is driving the app, and which Jira credentials their writes go out under.

On a shared server "who am I" cannot come from the machine any more, so each person is
given their own Jira API token and the app is configured with one entry per person. Signing
in picks that entry; from then on every comment, transition and assignment this session
makes is attributed in Jira to that person rather than to a single shared service account.

Users are defined in config/users.json:

    {
      "users": [
        {
          "id": "jane",
          "name": "Jane Smith",
          "email": "jane.smith@example.com",
          "apiTokenEnvironmentVariable": "JIRA_API_TOKEN_JANE"
        }
      ]
    }

The token itself is never stored in that file - `apiTokenEnvironmentVariable` names the
environment variable holding it, so tokens stay in the .env / Docker secret and the user
list can be read by anyone. `apiToken` is accepted inline as a fallback for quick local
testing, and `accessCode` (optional, also resolvable via `accessCodeEnvironmentVariable`)
makes signing in as that person require a shared code rather than just picking their name.
"""

import json
import os
from dataclasses import dataclass
from functools import wraps
from typing import Dict, List, Optional

from flask import jsonify, redirect, request, session, url_for

import settings

SESSION_USER_ID_KEY = 'user_id'


@dataclass(frozen=True)
class User:
    """One person who can sign in, and the Jira identity their writes are made under."""

    identifier: str
    name: str
    email: str
    api_token: str
    access_code: str

    @property
    def has_credentials(self) -> bool:
        return bool(self.email and self.api_token)

    @property
    def requires_access_code(self) -> bool:
        return bool(self.access_code)


class UserDirectoryError(RuntimeError):
    """The configured user list is missing or unusable."""


def _resolve_secret(entry: Dict[str, object], inline_key: str,
                    environment_variable_key: str) -> str:
    """Take a secret from the named environment variable, falling back to an inline value."""
    environment_variable_name = str(entry.get(environment_variable_key) or '').strip()
    if environment_variable_name:
        from_environment = os.environ.get(environment_variable_name, '').strip()
        if from_environment:
            return from_environment
    return str(entry.get(inline_key) or '').strip()


def load_users() -> List[User]:
    """Read the configured users. Raises if the file is missing, so misconfiguration is loud."""
    users_file = settings.USERS_FILE
    if not users_file.is_file():
        raise UserDirectoryError(
            f'No user list at {users_file}. Copy config/users.example.json to '
            f'config/users.json and add one entry per person.')
    try:
        raw_document = json.loads(users_file.read_text(encoding='utf-8'))
    except json.JSONDecodeError as decode_error:
        raise UserDirectoryError(f'{users_file.name} is not valid JSON: {decode_error}') from None

    users: List[User] = []
    for entry in raw_document.get('users', []):
        name = str(entry.get('name') or '').strip()
        if not name:
            continue
        identifier = str(entry.get('id') or name).strip().lower().replace(' ', '-')
        users.append(User(
            identifier=identifier,
            name=name,
            email=str(entry.get('email') or '').strip(),
            api_token=_resolve_secret(entry, 'apiToken', 'apiTokenEnvironmentVariable'),
            access_code=_resolve_secret(entry, 'accessCode', 'accessCodeEnvironmentVariable'),
        ))
    if not users:
        raise UserDirectoryError(f'{users_file.name} lists no users.')
    return users


def find_user(identifier: str) -> Optional[User]:
    wanted = (identifier or '').strip().lower()
    return next((user for user in load_users() if user.identifier == wanted), None)


def current_user() -> Optional[User]:
    """The signed-in user, or None. A user removed from the list is treated as signed out."""
    identifier = session.get(SESSION_USER_ID_KEY)
    if not identifier:
        return None
    try:
        return find_user(identifier)
    except UserDirectoryError:
        return None


def sign_in(user: User) -> None:
    session[SESSION_USER_ID_KEY] = user.identifier
    session.permanent = True


def sign_out() -> None:
    session.pop(SESSION_USER_ID_KEY, None)


def selectable_users() -> List[Dict[str, object]]:
    """The sign-in list for the UI. Deliberately carries no tokens - only who can sign in
    and whether an access code will be asked for."""
    return [{'id': user.identifier,
             'name': user.name,
             'email': user.email,
             'requiresAccessCode': user.requires_access_code,
             'configured': user.has_credentials}
            for user in load_users()]


def login_required(view_function):
    """Refuse anything but the sign-in pages until someone has said who they are.

    API calls get a 401 with a flag the page uses to bounce to the log-in screen; ordinary
    page loads are redirected there directly.
    """
    @wraps(view_function)
    def guarded_view(*positional_arguments, **keyword_arguments):
        if current_user() is None:
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Not signed in.', 'signedOut': True}), 401
            return redirect(url_for('login_page', next=request.path))
        return view_function(*positional_arguments, **keyword_arguments)

    return guarded_view
