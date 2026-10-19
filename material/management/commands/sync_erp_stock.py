"""Reconcile current stock without waiting for a full-year ledger import."""
import json
import time
from django.core.management.base import BaseCommand, CommandError
from material.erp_sync_lock import ERPSyncBusy, erp_sync_lock


class Command(BaseCommand):
    help = 'ERP 현재고 기준 즉시 보정 (수불 이력 가져오기와 독립 실행, LOT 보정 이력 유지)'

    def add_arguments(self, parser):
        parser.add_argument('--wait-seconds', type=int, default=0,
                            help='기존 ERP 작업의 잠금 해제를 기다릴 최대 시간 (0~7200초)')

    def handle(self, *args, **options):
        from material.erp_api import sync_stock_from_erp
        if not 0 <= options['wait_seconds'] <= 7200:
            raise CommandError('--wait-seconds는 0~7200 사이여야 합니다.')
        deadline = time.monotonic() + options['wait_seconds']
        announced = False
        while True:
            try:
                with erp_sync_lock():
                    result = sync_stock_from_erp()
                break
            except ERPSyncBusy as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CommandError(f'보정 미실행: {exc}') from exc
                if not announced:
                    self.stdout.write('기존 ERP 작업이 끝나기를 기다립니다. 잠금을 강제로 해제하지 않습니다.')
                    announced = True
                time.sleep(min(5, remaining))
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        if result.get('error'):
            raise CommandError('일부 또는 전체 보정 확인 필요: ' + result['error'])
        self.stdout.write(self.style.SUCCESS(f'ERP 현재고 보정 완료: {result["adjusted"]}건'))
