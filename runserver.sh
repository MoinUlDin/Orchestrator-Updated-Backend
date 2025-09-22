#!/usr/bin/env bash
set -euo pipefail

# If script is in project root, cd there; otherwise adjust as needed.
cd "$(dirname "$0")"

echo "Collect static files..."
python manage.py collectstatic --no-input

echo "Make migrations (dev only - consider removing for prod)"
python manage.py makemigrations

echo "Apply migrations"
python manage.py migrate

echo "Start APScheduler in background"
# Optional: export RUNNING_EXTERNAL_SCHEDULER=1 if your AppConfig.ready() checks it
export RUNNING_EXTERNAL_SCHEDULER=1
python manage.py runapscheduler &

# Use DJANGO_WSGI env var if provided, else default to orchestrator.wsgi:application
WSGI_MODULE="${DJANGO_WSGI:-orchestrator.wsgi:application}"

echo "Starting Gunicorn with WSGI: $WSGI_MODULE"
exec gunicorn "$WSGI_MODULE" \
  --bind 0.0.0.0:80 \
  --workers 3 \
  --timeout 120 \
  --log-level info \
  --chdir "$(pwd)"
