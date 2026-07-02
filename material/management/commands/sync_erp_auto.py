"""
ERP 자동 동기화 (입고/생산출고/생산입고/재고이동/재고조정/고객출고)
기존 apps.py 백그라운드 스레드를 대체 — cron으로 호출

crontab: */10 * * * * cd /var/www/scm_project && venv/bin/python manage.py sync_erp_auto >> /var/log/scm_erp_sync.log 2>&1
"""
import logging
import fcntl
import tempfile
import os

from django.core.management.base import BaseCommand
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

LOCK_FILE = os.path.join(tempfile.gettempdir(), 'scm_erp_auto_sync.lock')


class Command(BaseCommand):
    help = 'ERP 6종 자동 동기화 (cron 호출용)'

    def handle(self, *args, **options):
        from django.conf import settings

        if not getattr(settings, 'ERP_ENABLED', False):
            self.stdout.write('ERP 비활성화 상태, 건너뜀')
            return

        # 파일 락으로 동시 실행 방지 (cron 겹침 대비)
        lock_fp = open(LOCK_FILE, 'w')
        try:
            fcntl.flock(lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stdout.write('다른 동기화 프로세스 실행 중, 건너뜀')
            lock_fp.close()
            return

        try:
            from material.erp_api import (
                sync_erp_incoming, sync_erp_issue, sync_erp_receipt,
                sync_erp_stock_transfer, sync_erp_adjustments, sync_erp_outgoing,
            )

            sync_jobs = [
                ('incoming',  '입고',    sync_erp_incoming),
                ('issue',     '생산출고', sync_erp_issue),
                ('receipt',   '생산입고', sync_erp_receipt),
                ('transfer',  '재고이동', sync_erp_stock_transfer),
                ('adjust',    '재고조정', sync_erp_adjustments),
                ('outgoing',  '고객출고', sync_erp_outgoing),
            ]

            now_str = timezone.now().strftime('%Y-%m-%d %H:%M')

            for key, label, func in sync_jobs:
                try:
                    synced, skipped, errors, error_list = func()
                    cache.set(f'erp_{key}_sync_result', {
                        'synced': synced,
                        'skipped': skipped,
                        'errors': errors,
                        'error_list': error_list[:5],
                        'finished_at': now_str,
                        'auto': True,
                    }, timeout=86400)
                    if synced > 0:
                        logger.info(f'ERP {label} 동기화: 신규 {synced}건, 건너뜀 {skipped}건, 오류 {errors}건')
                        self.stdout.write(self.style.SUCCESS(f'  {label}: 신규 {synced}건'))
                except Exception as e:
                    logger.error(f'ERP {label} 동기화 오류: {e}', exc_info=True)
                    self.stdout.write(self.style.ERROR(f'  {label} 오류: {e}'))
                    cache.set(f'erp_{key}_sync_result', {
                        'synced': 0, 'skipped': 0, 'errors': 1,
                        'error_list': [str(e)],
                        'finished_at': now_str,
                        'auto': True,
                    }, timeout=86400)

        finally:
            fcntl.flock(lock_fp, fcntl.LOCK_UN)
            lock_fp.close()
