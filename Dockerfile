FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 ticketwatch \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin ticketwatch

WORKDIR /app

COPY --chown=ticketwatch:ticketwatch . .
RUN pip install --no-cache-dir .


FROM base AS test

RUN pip install --no-cache-dir ".[dev]"

USER ticketwatch


FROM base AS web

RUN python manage.py collectstatic --noinput

USER ticketwatch

CMD ["gunicorn", "ticketwatch.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "2", "--timeout", "60", "--access-logfile", "-", "--error-logfile", "-"]


FROM base AS worker

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        dbus-x11 \
        gnome-keyring \
        libsecret-1-0 \
        nodejs \
        npm \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global "@tencent-qqmail/agently-cli@1.0.15" \
    && npm cache clean --force

USER ticketwatch

ENTRYPOINT ["/app/docker/worker-entrypoint.sh"]
CMD ["python", "manage.py", "runworker"]
