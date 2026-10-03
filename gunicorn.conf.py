"""Gunicorn settings for Delivery Helper.

Threads rather than processes, deliberately: the app keeps its Jira issue cache in module
memory, and one worker means one cache that every request shares. Extra processes would
each hold their own copy and multiply the load on Jira for no gain - a handful of testers
does not need more than one worker, and the work is all network-bound anyway.
"""

import os

bind = f"{os.environ.get('UAT_HOST', '0.0.0.0')}:{os.environ.get('UAT_PORT', '9090')}"

workers = 1
threads = int(os.environ.get('UAT_THREADS', '8'))
worker_class = 'gthread'

# A cold start pulls ~1,200 issues from Jira, and a full export pulls more again, so the
# default 30s timeout is far too short for this app's slowest requests.
timeout = int(os.environ.get('UAT_TIMEOUT_SECONDS', '300'))
graceful_timeout = 30
keepalive = 5

accesslog = '-'
errorlog = '-'
loglevel = os.environ.get('UAT_LOG_LEVEL', 'info')

# Behind a reverse proxy this makes url_for build https:// links rather than http://.
forwarded_allow_ips = os.environ.get('UAT_FORWARDED_ALLOW_IPS', '127.0.0.1')
