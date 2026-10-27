"""성형 원단위 중량 관리: 월별 품번·재질·원재료별 수지 사용량과 스크랩.

출처
- 생산수(양품/불량), 파지 중량: ERP 생산실적 API. 파지는 비고(remarkDc)의 숫자 g (M7번대 4자리, 그 외 5자리)
- 제품중량·런너중량·C/V·총소요량·재료: 성형 마스터 (품번 × 재료구분 행).
  마스터에 없는 품번은 BOM의 원재료(ZR) 필요수량(kg/EA)을 쓴다. BOM 수량은 런너를 포함한 총소요량이라
  제품·런너 중량을 나눌 수 없어, 런너가 양품·불량 중량에 포함된다 (스크랩률이 낮게 나옴).
  단 M7번대 설비(핫런너·1캐비티) 품번은 런너가 없어 BOM 소요량이 곧 제품중량이다
- 검사불량: 3공장 창고(3000)에서 불량창고로 옮긴 이동 수량 (ERP 재고이동 동기화 + SCM 이동).
  조립 현장 등 다른 창고에서 나온 불량은 성형 원단위에 넣지 않는다

계산 (재료 행마다, kg)
- 양품 = (양품수 − 검사불량수) × 제품중량. 검사불량은 생산실적에 양품으로 잡힌 뒤 나온 것이라 양품에서 빼서 스크랩으로 옮긴다
- 공정불량/검사불량 = 수량 × 제품중량, 런너 = 생산수 × 런너중량 ÷ C/V
- 파지 = 품번의 비고 합계를 재료 행의 총소요량 비율로 나눔
- 총 사용량 = 양품 + 불량 + 런너 + 파지 (= 실제 투입량), 스크랩 = 불량 + 런너 + 파지
- 이 달 생산이 없는 품번의 검사불량은 이전 생산분이라 품번별 표·합계에 넣지 않고 따로 보여준다
"""
import re
from collections import defaultdict
from datetime import date, datetime

from django.conf import settings
from django.core.cache import cache
from django.db.models import Sum
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone

from orders.decorators import wms_permission_required
from .erp_api import call_erp_api, fetch_erp_bom
from .models import BOMItem, MaterialTransaction, MoldingMaster, ResinType

WR_API = '/apiproxy/api20A03S00901'
CACHE_SECONDS = 30 * 60
BOM_CACHE_SECONDS = 24 * 3600
RUNNERLESS_EQUIP = 'M7'  # 런너 없는 1캐비티 설비 (M702, M703 …)
DEFECT_FROM_WAREHOUSES = ('3000',)  # 검사불량으로 셀 불량창고 이동의 출발 창고 (3공장 창고)
SCRAP_RE = re.compile(r'^(?:[A-Za-z]\d{3}\s+)?(\d{4,5})$')  # '01475', '6100', 과거 엑셀 형식 'A670 01036'
KNOWN_RESINS = {'ABS', 'ASA', 'PA', 'PA6', 'PA12', 'PA46', 'PA66', 'PBT', 'PC', 'PC+ABS', 'PMMA', 'POM', 'PP',
                'TPE', 'TPO', 'TPU', 'M/B'}
MATERIAL_ORDER = {'재료1': 0, '재료2': 1, 'M/B': 2}


def parse_scrap(remark, equip):
    """비고에서 파지 중량(g)을 읽는다. (g, 확인 사유 또는 None)"""
    text = str(remark or '').strip()
    if not text:
        return 0, None
    m = SCRAP_RE.match(text)
    if not m:
        return 0, '숫자 형식 아님'
    digits = m.group(1)
    expected = 4 if equip.startswith('M7') else 5
    issue = None if len(digits) == expected else f'{len(digits)}자리 (설비 {equip}는 {expected}자리)'
    return int(digits), issue


def month_range(year, month):
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def fetch_results(year, month, refresh=False):
    """ERP 생산실적(성형 설비 M*)을 월 단위로 조회. 30분 캐시. ({'rows', 'fetched_at'}, 오류)"""
    key = f'resin_yield:wr:{year}{month:02d}'
    if not refresh:
        cached = cache.get(key)
        if cached is not None:
            return cached, None
    start, end = month_range(year, month)
    last = date.fromordinal(end.toordinal() - 1)
    ok, raw, err = call_erp_api(WR_API, {'coCd': settings.ERP_COMPANY_CODE,
                                         'wrDtFrom': start.strftime('%Y%m%d'), 'wrDtTo': last.strftime('%Y%m%d')})
    if not ok:
        return None, err or 'ERP 생산실적 조회 실패'
    rows = []
    for r in (raw or {}).get('resultData', []) or []:
        equip = (r.get('equipNm') or '').strip()
        part_no = (r.get('itemCd') or '').strip()
        if not re.match(r'^M\d', equip) or not part_no:
            continue
        rows.append({
            'date': r.get('wrDt', ''), 'equip': equip, 'part_no': part_no,
            'part_name': (r.get('itemNm') or '').strip(), 'wr_no': r.get('wrCd', ''),
            'good': float(r.get('goodQt') or 0), 'bad': float(r.get('badQt') or 0),
            'remark': (r.get('remarkDc') or '').strip(),
        })
    data = {'rows': rows, 'fetched_at': timezone.localtime().strftime('%Y-%m-%d %H:%M')}
    cache.set(key, data, CACHE_SECONDS)
    return data, None


def inspection_defects(year, month):
    """3공장 창고에서 불량창고로 옮긴 품번별 수량 (재고 맞춤 기록은 제외)."""
    start, end = month_range(year, month)
    tz = timezone.get_current_timezone()
    qs = MaterialTransaction.objects.filter(
        transaction_type__in=('TRF_ERP', 'TRANSFER'), warehouse_to__name__contains='불량',
        warehouse_from__code__in=DEFECT_FROM_WAREHOUSES,
        date__gte=datetime(start.year, start.month, 1, tzinfo=tz),
        date__lt=datetime(end.year, end.month, 1, tzinfo=tz),
    ).values('part__part_no').annotate(qty=Sum('quantity'))
    return {row['part__part_no']: float(row['qty'] or 0) for row in qs}


def resin_of(code, name, material_type, mapping):
    """재질: 재질 표(재료품번 정확히 일치. ZR253-D ≠ ZR253) → M/B → 재료명 첫 단어."""
    if code in mapping:
        return mapping[code]
    if material_type == 'M/B':
        return 'M/B'
    first = (name or '').split(' ')[0].upper()
    first = 'M/B' if first == 'MB' else first
    return first if first in KNOWN_RESINS else '미분류'


def master_specs(part_nos):
    specs = defaultdict(list)
    for m in MoldingMaster.objects.filter(part_no__in=part_nos):
        specs[m.part_no].append({
            'source': 'MASTER', 'sort': (MATERIAL_ORDER.get(m.material_type, 9), m.pk), 'part_name': m.part_name,
            'material_type': m.material_type, 'material_part_no': m.material_part_no,
            'material_name': m.material_name, 'cavity': m.cavity or 0, 'product_weight': float(m.product_weight),
            'runner_weight': float(m.runner_weight), 'grams': float(m.total_material),
        })
    return specs


def bom_specs(part_nos):
    """성형 마스터에 없는 품번의 원재료(ZR) 소요량: SCM에 동기화된 BOM, 없으면 ERP BOM (하루 캐시)."""
    found = defaultdict(list)
    for item in BOMItem.objects.filter(product__part_no__in=part_nos, child_part_no__startswith='ZR',
                                       is_active=True, is_bom_active=True).select_related('product'):
        qty = float(item.required_qty or item.net_qty or 0)
        if qty > 0:
            found[item.product.part_no].append((item.child_part_no, item.child_part_name, qty))
    for part_no in set(part_nos) - set(found):
        key = f'resin_yield:bom:{part_no}'
        children = cache.get(key)
        if children is None:
            ok, items, _ = fetch_erp_bom(part_no)
            if not ok:
                continue
            children = [(i.get('itemchildCd', ''), i.get('itemchildNm', ''),
                         float(i.get('realQt') or i.get('justQt') or 0))
                        for i in items or [] if str(i.get('itemchildCd', '')).startswith('ZR')]
            cache.set(key, children, BOM_CACHE_SECONDS)
        found[part_no].extend(c for c in children if c[2] > 0)
    return {part_no: [{
        'source': 'BOM', 'sort': (i,), 'part_name': '', 'material_type': 'BOM', 'material_part_no': code,
        'material_name': name, 'cavity': None, 'product_weight': None, 'runner_weight': None, 'grams': qty * 1000,
    } for i, (code, name, qty) in enumerate(children)] for part_no, children in found.items() if children}


def _ratio(part, whole):
    return part / whole if whole else 0


def build_report(year, month, refresh=False):
    data, err = fetch_results(year, month, refresh)
    if err:
        return {'error': err}

    produced = defaultdict(lambda: {'good': 0.0, 'bad': 0.0, 'scrap_g': 0, 'name': '', 'equips': set()})
    issues = []
    for r in data['rows']:
        p = produced[r['part_no']]
        p['good'] += r['good']
        p['bad'] += r['bad']
        p['name'] = p['name'] or r['part_name']
        p['equips'].add(r['equip'])
        grams, issue = parse_scrap(r['remark'], r['equip'])
        p['scrap_g'] += grams
        if issue:
            issues.append({**r, 'issue': issue, 'grams': grams})

    defects = inspection_defects(year, month)
    part_nos = set(produced) | {k for k, v in defects.items() if v}
    specs = master_specs(part_nos)
    mapping = dict(ResinType.objects.values_list('material_part_no', 'resin_type'))
    # 마스터에 없고 이 달 성형 생산·파지가 있는 품번만 BOM으로 채운다 (불량창고 이동만 있는 품번은 성형품이 아닐 수 있음)
    need_bom = [k for k in part_nos if k not in specs and k in produced
                and (produced[k]['good'] + produced[k]['bad'] or produced[k]['scrap_g'])]
    specs.update(bom_specs(need_bom))

    lines, unregistered, prior_defects = [], [], []
    for part_no in sorted(part_nos):
        p = produced.get(part_no) or {'good': 0.0, 'bad': 0.0, 'scrap_g': 0, 'name': '', 'equips': set()}
        made = p['good'] + p['bad']
        defect_qty = defects.get(part_no, 0.0)
        part_specs = sorted(specs.get(part_no, []), key=lambda sp: sp['sort'])
        if not made and not p['scrap_g']:
            # 이 달 생산이 없는 품번의 검사불량 = 이전 생산분. 이 달 원단위와 무관해 따로 둔다
            for sp in part_specs:
                weight = sp['grams'] if sp['source'] == 'BOM' else sp['product_weight']
                prior_defects.append({
                    'part_no': part_no, 'part_name': sp['part_name'], 'material_part_no': sp['material_part_no'],
                    'resin': resin_of(sp['material_part_no'], sp['material_name'], sp['material_type'], mapping),
                    'defect_qty': defect_qty, 'kg': defect_qty * weight / 1000,
                })
            continue
        if not part_specs:
            if made or p['scrap_g']:  # 불량창고 이동만 있는 조립품 등 성형품이 아닌 품번은 무시
                unregistered.append({'part_no': part_no, 'part_name': p['name'], 'produced': made,
                                     'scrap_kg': p['scrap_g'] / 1000, 'defect_qty': defect_qty})
            continue
        total_grams = sum(sp['grams'] for sp in part_specs)
        runnerless = bool(p['equips']) and all(e.startswith(RUNNERLESS_EQUIP) for e in p['equips'])
        for sp in part_specs:
            from_bom = sp['source'] == 'BOM'
            if from_bom and runnerless:  # 런너 없는 1캐비티: BOM 소요량 = 제품중량
                sp = {**sp, 'product_weight': sp['grams'], 'runner_weight': 0.0, 'cavity': 1}
            runner_included = from_bom and not runnerless
            cavity = sp['cavity']
            # BOM은 런너 포함 총소요량뿐이라 수량에 그대로 곱한다
            weight = sp['grams'] if runner_included else sp['product_weight']
            runner = sp['runner_weight'] or 0
            share = _ratio(sp['grams'], total_grams) if total_grams else 1 / len(part_specs)
            row = {
                'part_no': part_no, 'part_name': sp['part_name'] or p['name'], 'source': sp['source'],
                'resin': resin_of(sp['material_part_no'], sp['material_name'], sp['material_type'], mapping),
                'material_type': sp['material_type'], 'material_part_no': sp['material_part_no'],
                'material_name': sp['material_name'], 'cavity': cavity, 'product_weight': sp['product_weight'],
                'runner_weight': sp['runner_weight'],
                'unit_standard': sp['grams'] if runner_included else weight + (runner / cavity if cavity else 0),
                'produced': made, 'good_qty': p['good'], 'bad_qty': p['bad'], 'defect_qty': defect_qty,
                'good_kg': max(p['good'] - defect_qty, 0) * weight / 1000,
                'bad_kg': p['bad'] * weight / 1000,
                'inspect_kg': defect_qty * weight / 1000,
                'runner_kg': made * runner / cavity / 1000 if cavity else 0,
                'scrap_kg': p['scrap_g'] / 1000 * share,
                'no_cavity': not from_bom and not cavity,
                'runner_included': runner_included,
            }
            row['loss_kg'] = row['bad_kg'] + row['inspect_kg'] + row['runner_kg'] + row['scrap_kg']
            row['total_kg'] = row['good_kg'] + row['loss_kg']
            row['loss_rate'] = _ratio(row['loss_kg'], row['total_kg'])
            row['loss_pct'] = row['loss_rate'] * 100
            row['unit_actual'] = row['total_kg'] * 1000 / made if made else None
            lines.append(row)

    bom_parts = sorted({r['part_no'] for r in lines if r['runner_included']})
    by_material = _summarize(lines, 'material_part_no')
    return {
        'lines': lines,
        'by_resin': _summarize(lines, 'resin'),
        'by_material': by_material,
        'unclassified': [g for g in by_material if g['resin'] == '미분류'],
        'totals': _summarize(lines, None)[0] if lines else None,
        'issues': issues,
        'unregistered': unregistered,
        'bom_parts': bom_parts,
        'prior_defects': prior_defects,
        'prior_defect_kg': sum(d['kg'] for d in prior_defects),
        'unregistered_produced': sum(u['produced'] for u in unregistered),
        'unregistered_scrap_kg': sum(u['scrap_kg'] for u in unregistered),
        'no_cavity': [r for r in lines if r['no_cavity'] and r['produced']],
        'fetched_at': data['fetched_at'],
        'erp_rows': len(data['rows']),
    }


KG_FIELDS = ('good_kg', 'bad_kg', 'inspect_kg', 'runner_kg', 'scrap_kg', 'loss_kg', 'total_kg')


def _summarize(lines, key):
    groups = {}
    for row in lines:
        k = row[key] if key else '합계'
        g = groups.setdefault(k, {'key': k, 'parts': set(), 'produced': 0.0, **{f: 0.0 for f in KG_FIELDS},
                                  'material_name': row['material_name'], 'resin': row['resin']})
        if row['part_no'] not in g['parts']:  # 재료가 여러 행인 품번의 생산수는 한 번만
            g['parts'].add(row['part_no'])
            g['produced'] += row['produced']
        for f in KG_FIELDS:
            g[f] += row[f]
    result = []
    for g in sorted(groups.values(), key=lambda g: -g['total_kg']):
        g['part_count'] = len(g.pop('parts'))
        g['loss_rate'] = _ratio(g['loss_kg'], g['total_kg'])
        g['loss_pct'] = g['loss_rate'] * 100
        result.append(g)
    return result


def _month_param(request):
    raw = (request.GET.get('month') or '').strip()
    try:
        picked = datetime.strptime(raw, '%Y-%m')
        return picked.year, picked.month
    except ValueError:
        today = timezone.localdate()
        return today.year, today.month


@wms_permission_required('can_wms_stock_view')
def resin_yield(request):
    year, month = _month_param(request)
    report = build_report(year, month, refresh=request.GET.get('refresh') == '1')
    return render(request, 'material/resin_yield.html', {
        'report': report, 'month': f'{year}-{month:02d}', 'year': year, 'mon': month,
        'resins': sorted({r['resin'] for r in report.get('lines', [])}),
        'issue_count': sum(len(report.get(k, [])) for k in ('issues', 'unregistered', 'unclassified')),
    })


@wms_permission_required('can_wms_stock_view')
def resin_yield_excel(request):
    from .resin_yield_excel import build_workbook

    year, month = _month_param(request)
    report = build_report(year, month)
    if report.get('error'):
        return HttpResponse(f"ERP 생산실적 조회 실패: {report['error']}", status=502,
                            content_type='text/plain; charset=utf-8')
    response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    filename = f'원단위 중량 관리기준 ({year}년 {month}월).xlsx'
    from urllib.parse import quote
    response['Content-Disposition'] = f"attachment; filename*=UTF-8''{quote(filename)}"
    build_workbook(report, year, month).save(response)
    return response
