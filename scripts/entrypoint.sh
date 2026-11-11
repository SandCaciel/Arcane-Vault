#!/bin/sh
set -e

REPO_URL="${GITHUB_REPO_URL:-https://github.com/SandCaciel/Arcane-Vault.git}"
BRANCH="${GITHUB_BRANCH:-main}"
SYNC_INTERVAL="${GIT_SYNC_INTERVAL_SECONDS:-60}"

echo "=== Arcane Vault Container Entrypoint ==="

# If running in Git-Sync mode and .git is present, pull latest commit on startup
if [ -d "/app/.git" ]; then
    echo "[Git-Sync] Pulling latest changes from ${REPO_URL} (${BRANCH})..."
    git fetch origin "${BRANCH}" && git reset --hard "origin/${BRANCH}" || echo "[Git-Sync] Initial pull skipped."
fi

mkdir -p /app/data /app/staticfiles

echo "[Django] Running database migrations..."
python manage.py migrate --noinput

echo "[Django] Collecting static files..."
python manage.py collectstatic --noinput

# Background Git-Poller Loop: checks GitHub every $SYNC_INTERVAL seconds
if [ "${ENABLE_GIT_POLLER:-true}" = "true" ] && [ -d "/app/.git" ]; then
    (
        while true; do
            sleep "${SYNC_INTERVAL}"
            git fetch origin "${BRANCH}" >/dev/null 2>&1 || continue
            LOCAL_SHA=$(git rev-parse HEAD)
            REMOTE_SHA=$(git rev-parse "origin/${BRANCH}")
            if [ "${LOCAL_SHA}" != "${REMOTE_SHA}" ]; then
                echo "[Git-Sync] New commit detected (${REMOTE_SHA}). Updating Arcane Vault..."
                git reset --hard "origin/${BRANCH}"
                pip install --no-cache-dir -q -r requirements.txt || true
                python manage.py migrate --noinput || true
                python manage.py collectstatic --noinput || true
                # Touch wsgi.py so Gunicorn reloads workers automatically
                touch /app/arcane_vault/wsgi.py
                echo "[Git-Sync] Update applied and workers reloaded."
            fi
        done
    ) &
fi

echo "[Server] Starting Gunicorn with auto-reload on port 8000..."
exec gunicorn arcane_vault.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers 3 \
    --reload \
    --timeout 120 \
    --access-logfile - \
    --error-logfile -
