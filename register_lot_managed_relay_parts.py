"""
검증된 "년주차+요일+순번" 형태의 숫자 LOT를 쓰는 릴레이 계열 품번들을
LOT 관리품목에 일괄 등록하는 일회성 스크립트.

check_lot_managed_candidates.py 결과에서 ①번 계열(숫자 LOT)만 선별함.
②번 알파벳+숫자 LOT 계열(구매 부품/커넥터)은 형태가 검증 안 됐으므로 제외.

사용법 (운영 서버에서):
    python register_lot_managed_relay_parts.py
"""
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from orders.models import Part
from material.models import ProductionLotItem

PART_NOS = [
    'RJBE1-160',
    'RJBE2-160',
    'KRJH1-167HL',
    'KTB1-009',
    'KTB1-007',
    'KTB1-004',
    'KTB1-008',
]

for part_no in PART_NOS:
    part = Part.objects.filter(part_no=part_no).first()
    if not part:
        print(f'[스킵] {part_no}: Part 마스터에 없음')
        continue

    item, created = ProductionLotItem.objects.get_or_create(
        part=part,
        defaults={'remark': 'JBE RELAY 계열 (숫자 LOT)', 'is_active': True}
    )
    if created:
        print(f'[등록] {part_no} ({part.part_name})')
    elif not item.is_active:
        item.is_active = True
        item.save(update_fields=['is_active'])
        print(f'[재활성화] {part_no} (기존 미사용 상태였음)')
    else:
        print(f'[이미 등록됨] {part_no}')

print('\n완료. WMS > 재고/수불 관리 > LOT 관리품목 화면에서 확인해보세요.')
