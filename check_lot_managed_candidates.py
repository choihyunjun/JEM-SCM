"""
ERP 생산입고(api20A02S00701) 내역에서 실제로 LOT번호(lotNb)가 채워져 들어오는
품번을 찾아서, "LOT 관리품목"에 등록할 후보를 뽑아주는 진단 스크립트.

로컬 개발 환경은 ERP API 인증(callerName)이 안 잡혀있어서 여기선 실행이
안 되고, ERP 자격증명이 유효한 운영 서버에서 실행해야 함.

사용법 (운영 서버에서):
    python check_lot_managed_candidates.py [일수]
    예) python check_lot_managed_candidates.py 90   # 최근 90일 조회 (기본 90일)
"""
import os
import sys
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from datetime import datetime, timedelta
from material.erp_api import fetch_erp_receipt_list

days = int(sys.argv[1]) if len(sys.argv) > 1 else 90
date_to = datetime.now().strftime('%Y%m%d')
date_from = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')

print(f'조회 기간: {date_from} ~ {date_to} ({days}일)')

ok, items, err = fetch_erp_receipt_list(date_from, date_to)
if not ok:
    print(f'조회 실패: {err}')
    sys.exit(1)

print(f'총 {len(items)}건 조회됨\n')

stats = {}
for it in items:
    item_cd = (it.get('itemCd') or '').strip()
    if not item_cd:
        continue
    lot_nb = (it.get('lotNb') or '').strip()
    s = stats.setdefault(item_cd, {
        'name': it.get('itemNm') or '',
        'total': 0,
        'with_lot': 0,
        'sample_lots': [],
    })
    s['total'] += 1
    if lot_nb:
        s['with_lot'] += 1
        if len(s['sample_lots']) < 5 and lot_nb not in s['sample_lots']:
            s['sample_lots'].append(lot_nb)

candidates = {k: v for k, v in stats.items() if v['with_lot'] > 0}

if not candidates:
    print('LOT번호가 채워진 생산입고 건이 하나도 없습니다.')
    sys.exit(0)

print(f'LOT번호가 입력되는 품번: {len(candidates)}개\n')
print(f'{"품번":<20}{"품명":<25}{"LOT건/전체":>12}  샘플 LOT')
print('-' * 100)
for item_cd, s in sorted(candidates.items(), key=lambda kv: -kv[1]['with_lot']):
    ratio = f'{s["with_lot"]}/{s["total"]}'
    samples = ', '.join(s['sample_lots'])
    print(f'{item_cd:<20}{s["name"][:23]:<25}{ratio:>12}  {samples}')

# 참고: LOT가 하나도 없는 품번 개수도 같이 알려줌 (전체 대비 비율 감 잡기용)
no_lot = {k: v for k, v in stats.items() if v['with_lot'] == 0}
print(f'\n(참고) LOT 없는 품번: {len(no_lot)}개 / 전체 품번: {len(stats)}개')
