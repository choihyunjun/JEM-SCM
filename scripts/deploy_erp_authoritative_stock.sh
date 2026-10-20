#!/usr/bin/env bash
# Linux/systemd deployment for the existing SQLite SCM server. Run as root.
set -euo pipefail
cd "$(dirname "$0")/.."
ERP_PYTHON="$PWD/venv/bin/python"
export DJANGO_SETTINGS_MODULE=config.settings
ERP_LOCK_FILE="$("$ERP_PYTHON" - <<'PY'
import os, tempfile
from pathlib import Path
from django.conf import settings
path = str(getattr(settings, 'ERP_SYNC_LOCK_PATH', Path(tempfile.gettempdir()) / 'scm_erp_inventory.lock'))
try:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDONLY, 0o644)
    os.close(fd)
except FileExistsError:
    pass
print(path)
PY
)"
(
    echo 'Waiting for the current ERP sync to finish before migrating (up to 2 hours)...'
    flock -w 7200 9
    systemctl stop gunicorn
    # Always bring the service back. A failed migration is atomic on SQLite, so
    # the previous schema remains; restore the printed backup only if needed.
    trap 'status=$?; systemctl start gunicorn; if [ "$status" -ne 0 ]; then
        echo "DEPLOY FAILED (exit $status). Gunicorn restarted. DB unchanged unless migrate reported OK." >&2
        echo "Restore if required: systemctl stop gunicorn; cp <backup above> <db.sqlite3>; git checkout <previous commit>; systemctl start gunicorn" >&2
    fi' EXIT
    "$ERP_PYTHON" - <<'PY'
import sqlite3
from datetime import datetime
from pathlib import Path
from django.conf import settings
db = settings.DATABASES['default']
if db['ENGINE'] != 'django.db.backends.sqlite3':
    raise SystemExit('This deployment helper requires SQLite. Use the appropriate DB backup procedure.')
source = Path(db['NAME']).resolve()
if not source.is_file():
    raise SystemExit('Database file not found; migration aborted.')
folder = Path('/var/backups/scm_project')
folder.mkdir(parents=True, exist_ok=True, mode=0o700)
target = folder / ('before-stock-decimal-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.sqlite3')
with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src:
    with sqlite3.connect(target) as dst:
        src.backup(dst)
target.chmod(0o600)
print('Database backup:', target)
PY
    "$ERP_PYTHON" manage.py migrate --noinput
    "$ERP_PYTHON" manage.py check_deployment
    "$ERP_PYTHON" manage.py collectstatic --noinput
) 9<"$ERP_LOCK_FILE"
# Type=notify services report "activating" for a few seconds after start.
for _ in $(seq 1 30); do
    systemctl is-active --quiet gunicorn && break
    sleep 1
done
systemctl is-active gunicorn
"$ERP_PYTHON" manage.py sync_erp_stock --wait-seconds 7200
