"""ERP 수불 반영 후 현재고 보정 (cron 호출용)."""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from material.erp_sync_pipeline import sync_inventory
from material.erp_sync_lock import ERPSyncBusy


class Command(BaseCommand):
    help = 'ERP 6종 수불 동기화 후 총량 보정'

    def handle(self, *args, **options):
        if not getattr(settings, 'ERP_ENABLED', False):
            self.stdout.write('ERP 비활성화 상태, 건너뜀')
            return
        try:
            result = sync_inventory()
        except ERPSyncBusy as exc:
            self.stdout.write(str(exc))
            return
        if result['errors']:
            raise CommandError('; '.join(result['error_list']) or '수불 동기화 오류')
        self.stdout.write(self.style.SUCCESS(
            f"수불 {result['synced']}건 / 재고 조정 {result['stock']['adjusted']}건 완료"
        ))
