"""Every path and tunable the server needs, resolved once from the environment.

The container keeps code and data strictly apart: code is baked into the image and is
read-only, while everything the app writes (exports, generated email drafts, the
day-over-day history snapshots) lives under a single mounted data directory so it survives
a redeploy. Both roots are overridable, so the app still runs straight from a checkout on
Windows.

Importing this module loads the .env file first, before anything reads os.environ - the
per-user Jira tokens are looked up by name at start-up, so they have to be in place by
then.
"""

import os
import sys
from pathlib import Path

APPLICATION_DIRECTORY = Path(__file__).resolve().parent
PROJECT_ROOT_DIRECTORY = APPLICATION_DIRECTORY.parent

ENVIRONMENT_FILE = Path(os.environ.get('UAT_ENVIRONMENT_FILE')
                        or PROJECT_ROOT_DIRECTORY / '.env')


def load_environment_file(environment_path: Path = ENVIRONMENT_FILE) -> None:
    """Populate os.environ from a KEY=VALUE .env file. Existing variables win.

    Under Docker the values normally arrive as real environment variables (env_file in
    the compose file), and this is a no-op. Running straight from a checkout, this is
    what supplies them.
    """
    if not environment_path.is_file():
        return
    for line_number, raw_line in enumerate(
            environment_path.read_text(encoding='utf-8').splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        if '=' not in line:
            print(f'WARNING: ignoring malformed line {line_number} in {environment_path.name}',
                  file=sys.stderr)
            continue
        name, _, value = line.partition('=')
        os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


load_environment_file()


def _directory_from_environment(variable_name: str, default_directory: Path) -> Path:
    configured = os.environ.get(variable_name, '').strip()
    return Path(configured) if configured else default_directory


# Written to at runtime. Mounted as a volume in Docker.
DATA_DIRECTORY = _directory_from_environment(
    'UAT_DATA_DIRECTORY', PROJECT_ROOT_DIRECTORY / 'data')

# Read at runtime but never written: the project definition, the tester directory, the
# user log-in list and any files attached to tester emails.
CONFIG_DIRECTORY = _directory_from_environment(
    'UAT_CONFIG_DIRECTORY', PROJECT_ROOT_DIRECTORY / 'config')

# Shipped with the image: the status screenshots shown inline in tester emails.
ASSETS_DIRECTORY = APPLICATION_DIRECTORY / 'assets'
EMAIL_IMAGE_DIRECTORY = ASSETS_DIRECTORY / 'images'

# Deployment-specific files attached to every tester email (guides, reference data). They
# belong to one organisation's rollout, not to the app, so they live with the config.
ATTACHMENT_DIRECTORY = CONFIG_DIRECTORY / 'attachments'

EXPORT_DIRECTORY = DATA_DIRECTORY / 'exports'
EMAIL_OUTPUT_DIRECTORY = DATA_DIRECTORY / 'emails'
HISTORY_DIRECTORY = DATA_DIRECTORY / 'history'

PROJECT_FILE = _directory_from_environment('UAT_PROJECT_FILE', CONFIG_DIRECTORY / 'project.json')
TESTERS_FILE = CONFIG_DIRECTORY / 'testers.json'
USERS_FILE = _directory_from_environment('UAT_USERS_FILE', CONFIG_DIRECTORY / 'users.json')

# Signs the log-in cookie. Generated per-container if unset, which works for a single
# replica but signs everyone out on restart, so a deployment should pin it in the .env.
FLASK_SECRET_KEY = os.environ.get('UAT_SECRET_KEY') or os.urandom(32).hex()

# Shown in the page header, the browser tab and the start-up banner.
APPLICATION_NAME = os.environ.get('UAT_APPLICATION_NAME', '').strip() or 'Delivery Helper'

LISTEN_HOST = os.environ.get('UAT_HOST', '0.0.0.0')
LISTEN_PORT = int(os.environ.get('UAT_PORT', '9090'))

# Concurrent request handlers. The work is network-bound (waiting on Jira), so threads are
# the right unit; gunicorn.conf.py reads the same variable in the container.
SERVER_THREADS = int(os.environ.get('UAT_THREADS', '8'))

# Testers change issues in Jira while this app is open, so cached data is only trusted
# briefly. Anything older is refetched on the next request.
CACHE_MAX_AGE_SECONDS = int(os.environ.get('UAT_CACHE_MAX_AGE_SECONDS', '25'))


def ensure_writable_directories() -> None:
    """Create the data directories up front, so the first write cannot fail on a fresh volume."""
    for directory in (DATA_DIRECTORY, EXPORT_DIRECTORY, EMAIL_OUTPUT_DIRECTORY,
                      HISTORY_DIRECTORY):
        directory.mkdir(parents=True, exist_ok=True)
