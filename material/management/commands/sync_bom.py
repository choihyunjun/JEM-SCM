"""
ERP BOM 동기화 관리 커맨드
- 더존 아마란스10 ERP에서 BOM 데이터를 가져와 DB 업데이트
- 전체: python manage.py sync_bom
- 단일: python manage.py sync_bom --part-no 064133-0010
- 24시간 자동 실행: cron 또는 Windows 작업 스케줄러
"""

import time
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'ERP에서 BOM 데이터를 동기화합니다'

    def add_arguments(self, parser):
        parser.add_argument(
            '--part-no',
            type=str,
            help='특정 모품만 동기화 (미지정 시 전체)',
        )

    def handle(self, *args, **options):
        part_no = options.get('part_no')

        if part_no:
            self._sync_single(part_no)
        else:
            self._sync_all()

    def _sync_single(self, part_no):
        from material.erp_api import sync_single_bom

        self.stdout.write(f'모품 [{part_no}] BOM 동기화 시작...')

        ok, count, err = sync_single_bom(part_no)
        if ok:
            self.stdout.write(self.style.SUCCESS(f'완료: 자품 {count}개 동기화'))
        else:
            self.stderr.write(self.style.ERROR(f'실패: {err}'))

    def _sync_all(self):
        from material.erp_api import sync_all_bom
        from material.models import Product
        from django.core.cache import cache
        from django.utils import timezone

        total = Product.objects.filter(is_active=True).count()
        self.stdout.write(f'전체 BOM 동기화 시작 (모품 {total}건)...')
        self.stdout.write('ERP API 호출량이 많으므로 시간이 걸릴 수 있습니다.')

        start = time.time()
        synced, skipped, errors, error_list = sync_all_bom()
        elapsed = time.time() - start

        # 동기화 시간 기록
        cache.set('bom_last_sync', timezone.now().strftime('%Y-%m-%d %H:%M'), timeout=None)

        self.stdout.write('')
        self.stdout.write(f'동기화 완료 ({elapsed:.1f}초)')
        self.stdout.write(self.style.SUCCESS(f'  성공: {synced}건'))
        self.stdout.write(f'  건너뜀: {skipped}건 (ERP에 BOM 없음)')
        if errors:
            self.stdout.write(self.style.ERROR(f'  오류: {errors}건'))
            for err in error_list[:10]:
                self.stderr.write(f'    - {err}')
            if len(error_list) > 10:
                self.stderr.write(f'    ... 외 {len(error_list) - 10}건')
