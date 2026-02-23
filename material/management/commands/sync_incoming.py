"""
ERP 입고정보 역방향 동기화 관리 커맨드
- 더존 아마란스10 ERP에서 입고 내역을 가져와 WMS에 동기화
- 기본: python manage.py sync_incoming (어제~오늘)
- 기간: python manage.py sync_incoming --days 7
- 지정: python manage.py sync_incoming --from 20260201 --to 20260219
"""

import time
from datetime import datetime, timedelta
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'ERP에서 입고 내역을 WMS에 동기화합니다'

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=None,
            help='최근 N일간 동기화 (기본: 어제~오늘)',
        )
        parser.add_argument(
            '--from',
            type=str,
            dest='date_from',
            help='시작일 (YYYYMMDD)',
        )
        parser.add_argument(
            '--to',
            type=str,
            dest='date_to',
            help='종료일 (YYYYMMDD)',
        )

    def handle(self, *args, **options):
        from material.erp_api import sync_erp_incoming

        date_from = options.get('date_from')
        date_to = options.get('date_to')
        days = options.get('days')

        if days:
            date_from = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')
            date_to = datetime.now().strftime('%Y%m%d')

        if date_from:
            self.stdout.write(f'ERP 입고 동기화 시작: {date_from} ~ {date_to or "오늘"}')
        else:
            self.stdout.write('ERP 입고 동기화 시작: 어제~오늘')

        start = time.time()
        synced, skipped, errors, error_list = sync_erp_incoming(date_from, date_to)
        elapsed = time.time() - start

        self.stdout.write('')
        self.stdout.write(f'동기화 완료 ({elapsed:.1f}초)')
        self.stdout.write(self.style.SUCCESS(f'  동기화: {synced}건'))
        self.stdout.write(f'  건너뜀: {skipped}건 (이미 존재 또는 SCM발)')
        if errors:
            self.stdout.write(self.style.ERROR(f'  오류: {errors}건'))
            for err in error_list[:10]:
                self.stderr.write(f'    - {err}')
            if len(error_list) > 10:
                self.stderr.write(f'    ... 외 {len(error_list) - 10}건')
