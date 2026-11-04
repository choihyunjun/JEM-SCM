"""
ERP BOM 동기화 관리 커맨드
- 더존 아마란스10 ERP에서 BOM 데이터를 가져와 DB 업데이트
- 전체: python manage.py sync_bom
- 단일: python manage.py sync_bom --part-no 064133-0010
- 반제품 누락 점검: python manage.py sync_bom --missing-sub [--apply]
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
        parser.add_argument(
            '--missing-sub',
            action='store_true',
            help='자품번 중 모품 등록이 빠진 반제품만 점검 (--apply 시 등록)',
        )
        parser.add_argument('--apply', action='store_true', help='--missing-sub 점검 결과를 실제 등록')

    def handle(self, *args, **options):
        part_no = options.get('part_no')

        if options.get('missing_sub'):
            self._missing_sub(options.get('apply'))
        elif part_no:
            self._sync_single(part_no)
        else:
            self._sync_all()

    def _missing_sub(self, apply):
        from material.erp_api import fetch_erp_bom, sync_sub_boms
        from material.models import BOMItem, Product

        products = set(Product.objects.values_list('part_no', flat=True))
        parents = {}
        for child, parent in BOMItem.objects.filter(is_active=True).values_list('child_part_no', 'product__part_no'):
            if child not in products:
                parents.setdefault(child, parent)
        self.stdout.write(f'모품 미등록 자품번 {len(parents)}건 ERP 조회 중...')

        found, failed = [], []
        for child in sorted(parents):
            ok, items, err = fetch_erp_bom(child)
            if not ok:
                failed.append(f'{child}({err})')
            elif items:
                found.append(child)
                self.stdout.write(f'  반제품 누락: {child} {items[0].get("itemparentNm", "")} '
                                  f'(상위 {parents[child]}, 자품 {len(items)}개)')
        self.stdout.write(f'누락 반제품 {len(found)}건, 조회 실패 {len(failed)}건')
        for f in failed[:10]:
            self.stderr.write(f'  조회 실패: {f}')

        if not apply or not found:
            if found:
                self.stdout.write('등록하려면 --apply 를 붙여 다시 실행하세요.')
            return
        synced, errors = sync_sub_boms(found)
        self.stdout.write(self.style.SUCCESS(f'등록 완료: {len(synced)}건 (하위 포함)'))
        for e in errors:
            self.stderr.write(f'  실패: {e}')

    def _sync_single(self, part_no):
        from material.erp_api import sync_single_bom

        self.stdout.write(f'모품 [{part_no}] BOM 동기화 시작...')

        ok, count, err = sync_single_bom(part_no)
        if ok:
            self.stdout.write(self.style.SUCCESS(f'완료: 자품 {count}개 동기화'))
            if err:
                self.stderr.write(self.style.WARNING(err))
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
