# Delivery Helper - Jira-backed UAT tracking.
#
# Nothing Windows-specific is in the app (email drafts are written as .eml files rather than
# through Outlook), so this is a plain slim-Python image.

FROM python:3.12-slim

# Faster, quieter, and no stale .pyc in the layer.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/delivery-helper

# Dependencies first, so a code change does not re-resolve the whole environment.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY gunicorn.conf.py ./
COPY app ./app

# The config (who signs in, the project definition, the tester directory) and the data
# (exports, drafts, history) are both mounted at run time; the defaults keep the image
# runnable on its own.
ENV UAT_CONFIG_DIRECTORY=/srv/delivery-helper/config \
    UAT_DATA_DIRECTORY=/data \
    PYTHONPATH=/srv/delivery-helper/app

RUN mkdir -p /data/exports /data/emails /data/history /srv/delivery-helper/config

# Run unprivileged. The data volume has to be writable by this user.
RUN useradd --system --create-home --uid 10001 deliveryhelper \
 && chown -R deliveryhelper:deliveryhelper /srv/delivery-helper /data
USER deliveryhelper

EXPOSE 9090

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9090/healthz', timeout=4).status == 200 else 1)"

CMD ["gunicorn", "--config", "gunicorn.conf.py", "app:application"]
