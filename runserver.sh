#!/usr/bin/env bash
set -euo pipefail

# go to project root (adjust if your Docker WORKDIR differs)
cd "$(dirname "$0")"

echo "Collect static files..."
python manage.py collectstatic --no-input

echo "Make migrations (dev only - consider removing for prod)"
python manage.py makemigrations

echo "Apply migrations"
python manage.py migrate

echo "Start APScheduler in background"
python manage.py runapscheduler &

# Use your project's WSGI module here (replace 'orchestrator' with your project package if different)
WSGI_MODULE="${DJANGO_WSGI:-orchestrator.wsgi:application}"

echo "Starting Gunicorn with WSGI: $WSGI_MODULE"
exec gunicorn "$WSGI_MODULE" \
  --bind 0.0.0.0:8000 \
  --workers 3 \
  --timeout 120 \
  --log-level info \
  --chdir "$(pwd)"
