"""One ERP importer at a time on this application host, including cron/web.

OS locks are released on worker termination; no expiring cache lease can let a
second importer enter while a slow first importer is still running. For multiple
application hosts this lock file must reside on a filesystem with shared locks.
"""
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
import os
import tempfile
import threading

from django.conf import settings

_local = threading.local()


class ERPSyncBusy(RuntimeError):
    pass


@contextmanager
def erp_sync_lock():
    if getattr(_local, 'held', False):
        yield
        return
    path = Path(getattr(settings, 'ERP_SYNC_LOCK_PATH',
                        Path(tempfile.gettempdir()) / 'scm_erp_inventory.lock'))
    # On Linux flock also works with a read-only descriptor. This permits root
    # cron and the web-service user to share a 0644 lock file safely.
    mode = os.O_RDWR if os.name == 'nt' else os.O_RDONLY
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | mode, 0o644)
    except FileExistsError:
        fd = os.open(path, mode)
    with os.fdopen(fd, 'r+b' if os.name == 'nt' else 'rb') as lock:
        if os.name == 'nt':
            import msvcrt
            if path.stat().st_size == 0:
                lock.write(b'0')
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ERPSyncBusy('다른 ERP 동기화가 실행 중입니다. 완료 후 다시 시도해 주세요.') from exc
        else:
            import fcntl
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ERPSyncBusy('다른 ERP 동기화가 실행 중입니다. 완료 후 다시 시도해 주세요.') from exc
        _local.held = True
        try:
            yield
        finally:
            _local.held = False
            if os.name == 'nt':
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def serialized_erp_sync(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        with erp_sync_lock():
            return func(*args, **kwargs)
    return wrapped


def erp_sync_is_running():
    if getattr(_local, 'held', False):
        return True
    try:
        with erp_sync_lock():
            return False
    except ERPSyncBusy:
        return True
