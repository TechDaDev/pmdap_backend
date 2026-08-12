#!/bin/sh
set -eu

python manage.py migrate --noinput
python manage.py collectstatic --noinput
# Railway injects PORT (default 8080); keep a local default for Compose.
exec gunicorn config.wsgi:application \
    --bind "0.0.0.0:${PORT:-8000}" \
    --workers 2 \
    --timeout 120 \
    --no-control-socket
