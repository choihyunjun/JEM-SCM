"""
ERP 과거 수불 변경 감지 (새벽 cron용)

사용법:
  python manage.py detect_past_changes          # 감지만 (캐시에 저장)
  python manage.py detect_past_changes --auto    # 감지 + 자동 보정

동작:
  기초재고 셋팅일 이전 기간의 ERP 수불이 사후 수정되었는지 감지.
  변경이 있으면 캐시에 저장 → ERP 재고관리 페이지에 경고 배너 표시.
  --auto 옵션 시 해당 품목 재고를 ERP에 맞춰 자동 보정.

cron 예시 (매일 새벽 1시):
  0 1 * * * cd /var/www/scm_project && /var/www/scm_project/venv/bin/python manage.py detect_past_changes --auto >> /var/log/scm_past_changes.log 2>&1
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = '기초재고 셋팅일 이전 ERP 수불 변경을 감지하고 선택적으로 보정합니다'

    def add_arguments(self, parser):
        parser.add_argument(
            '--auto',
            action='store_true',
            dest='auto_adjust',
            help='변경 감지 시 해당 품목 재고를 자동 보정',
        )

    def handle(self, *args, **options):
        from material.erp_api import detect_past_changes, adjust_stock_for_parts
        from django.core.cache import cache

        self.stdout.write(self.style.WARNING('\n=== ERP 과거 수불 변경 감지 ===\n'))

        result = detect_past_changes()

        if result.get('error'):
            self.stderr.write(self.style.ERROR(f'오류: {result["error"]}'))
            return

        self.stdout.write(f'조회 기간: {result["period"]}')
        self.stdout.write(f'감지된 변경: {result["count"]}건')
        self.stdout.write(f'영향 품목: {len(result.get("affected_parts", []))}개\n')

        if result['count'] == 0:
            cache.delete('erp_past_changes')
            self.stdout.write(self.style.SUCCESS('변경 없음. 정상입니다.'))
            return

        # 변경 내역 출력
        for c in result.get('changes', [])[:20]:
            type_label = {'added': '신규', 'deleted': '삭제', 'modified': '수정'}.get(c['type'], c['type'])
            self.stdout.write(
                f'  [{type_label}] {c["trx_type"]} {c["erp_no"]} '
                f'품번={c["part_no"]} ERP={c["erp_qty"]} SCM={c["scm_qty"]}'
            )
        if result['count'] > 20:
            self.stdout.write(f'  ... 외 {result["count"] - 20}건')

        # 캐시에 저장 (웹에서 경고 배너 표시용)
        cache.set('erp_past_changes', result, timeout=86400)
        self.stdout.write(self.style.WARNING(f'\n캐시에 저장됨 (24시간 유효)'))

        # --auto: 자동 보정
        if options['auto_adjust']:
            part_nos = result.get('affected_parts', [])
            if part_nos:
                self.stdout.write(self.style.WARNING(f'\n[자동 보정] {len(part_nos)}개 품목 재고 보정 중...\n'))
                adj_result = adjust_stock_for_parts(part_nos)
                if adj_result.get('error'):
                    self.stderr.write(self.style.ERROR(f'보정 실패: {adj_result["error"]}'))
                else:
                    self.stdout.write(self.style.SUCCESS(
                        f'보정 완료: 조정 {adj_result["adjusted"]}건 '
                        f'(증가 {adj_result["increased"]}, 감소 {adj_result["decreased"]})'
                    ))
                    # 보정 후 캐시 초기화
                    cache.delete('erp_past_changes')
        else:
            self.stdout.write('\n보정하려면: python manage.py detect_past_changes --auto')

        self.stdout.write(self.style.SUCCESS('\n=== 완료 ==='))
