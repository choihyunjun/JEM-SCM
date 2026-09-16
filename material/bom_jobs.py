"""Resumable BOM calculations. Each POST commits a bounded chunk atomically.

No in-process background threads: requests can reach different Gunicorn workers.
The browser drives progress; reopening a job resumes the saved calculation.
"""
import copy
import csv
import io
import logging
import math
import re
from collections import Counter, defaultdict
from datetime import date, timedelta

import openpyxl
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from orders.models import Part
from .models import BOMCalculationJob, BOMCalculationRow, BOMItem, MaterialStock, Product

logger = logging.getLogger(__name__)
MAX_PLANS = 100000


def parse_plans(upload):
    """Row/pivot CSV and XLSX; preserve dates, quoted CSV and fractional input policy."""
    name = upload.name.lower()
    workbook = None
    if name.endswith('.csv'):
        records = csv.reader(io.StringIO(upload.read().decode('utf-8-sig')))
    elif name.endswith('.xlsx'):
        workbook = openpyxl.load_workbook(upload, read_only=True, data_only=True)
        records = workbook.active.iter_rows(values_only=True)
    else:
        raise ValueError('CSV 또는 XLSX 파일을 선택해주세요.')
    plans = []
    try:
        headers = [str(v or '').strip() for v in next(records, [])]
        if '품번' not in headers:
            raise ValueError('품번 열을 찾을 수 없습니다.')
        dates = [(i, h[:10]) for i, h in enumerate(headers) if re.match(r'^\d{4}-\d{2}-\d{2}', h)]
        if not dates and not {'계획수량', '수량'}.intersection(headers):
            raise ValueError('계획수량 또는 수량 열을 찾을 수 없습니다.')
        for number, values in enumerate(records, 2):
            row = dict(zip(headers, values))
            part_no = str(row.get('품번') or '').strip()
            if not part_no or not re.search('[A-Za-z0-9]', part_no):
                continue
            entries = [(d, values[i]) for i, d in dates if i < len(values)] if dates else [
                (row.get('날짜') or row.get('필요일자') or '', row.get('계획수량') or row.get('수량') or 0)]
            for day, quantity in entries:
                try:
                    numeric = float(str(quantity).replace(',', ''))
                    if not math.isfinite(numeric):
                        raise ValueError
                    qty = int(numeric)
                except (ValueError, TypeError, OverflowError):
                    if quantity in (None, ''):
                        continue
                    raise ValueError(f'{number}행 {part_no}: 수량을 확인해주세요.')
                if qty <= 0:
                    continue
                if day:
                    if hasattr(day, 'strftime'):
                        day = day.strftime('%Y-%m-%d')
                    else:
                        day = str(day).strip().removesuffix('.0')
                        if len(day) == 8 and day.isdigit():
                            day = f'{day[:4]}-{day[4:6]}-{day[6:]}'
                    try:
                        day = date.fromisoformat(day).isoformat()
                    except ValueError:
                        raise ValueError(f'{number}행 {part_no}: 날짜를 확인해주세요.')
                plans.append({'part_no': part_no, 'qty': qty, 'need_date': day or ''})
                if len(plans) > MAX_PLANS:
                    raise ValueError('유효 계획이 100,000건을 초과합니다. 파일을 나눠주세요.')
    finally:
        if workbook:
            workbook.close()
    if not plans:
        raise ValueError('양수의 생산수량이 입력된 계획이 없습니다.')
    return sorted(plans, key=lambda p: p['need_date'] or '9999-12-31')


def build_snapshot(plans):
    """Four bulk queries, independent of the number of plan dates."""
    products = dict(Product.objects.filter(is_active=True).values_list('part_no', 'part_name'))
    children = defaultdict(list)
    for item in BOMItem.objects.filter(is_active=True, is_bom_active=True, product__is_active=True).order_by('seq', 'pk').values(
        'product__part_no', 'child_part_no', 'child_part_name', 'child_unit', 'required_qty', 'supply_type', 'vendor_name'):
        children[item.pop('product__part_no')].append(item)
    parts = {p['part_no']: p for p in Part.objects.values('part_no', 'vendor__name', 'part_group')}
    stocks = dict(MaterialStock.objects.filter(quantity__gt=0).values('part__part_no').annotate(total=Sum('quantity')).values_list('part__part_no', 'total'))
    templates, vendors, unlinked, missing = {}, Counter(), {}, []
    frequencies = Counter(p['part_no'] for p in plans)
    for root, count in frequencies.items():
        flat, structured = {}, []

        def walk(part_no, multiplier, path, level=1):
            if part_no in path:
                raise ValueError(f'BOM 순환 참조: {" → ".join((*path, part_no))}')
            if level > 40 or len(structured) > 20000:
                raise ValueError(f'{root}: BOM 구조가 너무 큽니다. 중복/순환 구성을 확인해주세요.')
            for item in children[part_no]:
                pn = item['child_part_no']
                semi = pn in products and bool(children[pn])
                entry = {k: item[k] for k in ('child_part_no', 'child_part_name', 'child_unit', 'supply_type')}
                entry.update(unit_qty=float(item['required_qty']), required_qty=float(item['required_qty']) * multiplier,
                             vendor_name=parts.get(pn, {}).get('vendor__name') or item['vendor_name'],
                             is_semi=semi, level=level)
                if not semi:
                    entry['parent_part_no'] = part_no if level > 1 else None
                    if pn not in flat:
                        flat[pn] = {k: v for k, v in entry.items() if k not in ('is_semi', 'level', 'parent_part_no')}
                        flat[pn]['seq'] = len(flat)
                    else:
                        flat[pn]['required_qty'] += entry['required_qty']
                structured.append(entry)
                if semi:
                    walk(pn, entry['required_qty'], (*path, part_no), level + 1)

        if root in products:
            walk(root, 1, ())
        templates[root] = {'part_name': products.get(root, '-'), 'items': list(flat.values()), 'structured_items': structured}
        if not structured:
            missing.append(root)
        for item in structured:
            if item['vendor_name']:
                vendors[item['vendor_name']] += count
            elif not item['is_semi']:
                pn = item['child_part_no']
                unlinked[pn] = {'part_no': pn, 'part_name': item['child_part_name']}
    metadata = {'vendors': dict(vendors), 'unlinked': list(unlinked.values()), 'missing': missing,
                'part_groups': {pn: parts.get(pn, {}).get('part_group', '') for pn in unlinked}}
    return templates, stocks, metadata


def create_job(owner, upload):
    plans = parse_plans(upload)
    templates, stocks, metadata = build_snapshot(plans)
    with transaction.atomic():
        # Temporary results are retained for seven days, independently of login sessions.
        BOMCalculationJob.objects.filter(owner=owner, created_at__lt=timezone.now() - timedelta(days=7)).delete()
        job = BOMCalculationJob.objects.create(owner=owner, total=len(plans), snapshot=templates, remaining=stocks,
            summary={**metadata, 'materials': 0, 'shortages': 0, 'missing_count': 0})
        BOMCalculationRow.objects.bulk_create([
            BOMCalculationRow(job=job, sequence=i, payload=plan, has_bom=bool(templates[plan['part_no']]['items']))
            for i, plan in enumerate(plans)
        ], batch_size=300)
    return job


def advance_job(job):
    if job.status == 'complete':
        return
    # A lease and revision fence permit safe retries after disconnection/worker restart.
    claimed = BOMCalculationJob.objects.filter(pk=job.pk, revision=job.revision).filter(
        Q(status__in=['pending', 'failed']) | Q(status='running', updated_at__lt=timezone.now() - timedelta(minutes=2))
    ).update(status='running', revision=job.revision + 1, error='', updated_at=timezone.now())
    if not claimed:
        return
    version = job.revision + 1
    try:
        remaining, summary = copy.deepcopy(job.remaining), copy.deepcopy(job.summary)
        rows = list(job.rows.filter(sequence__gte=job.completed).order_by('sequence')[:25])
        processed, size = [], 0
        for row in rows:
            plan = row.payload
            template = copy.deepcopy(job.snapshot[plan['part_no']])
            size += len(template['structured_items'])
            if processed and size > 2000:
                break
            before = {item['child_part_no']: remaining.get(item['child_part_no'], 0) for item in template['items']}
            for item in template['items']:
                pn = item['child_part_no']
                item['required_qty'] *= plan['qty']
                item['stock_qty'] = remaining.get(pn, 0)
                item['shortage'] = max(0, item['required_qty'] - item['stock_qty'])
                remaining[pn] = max(0, item['stock_qty'] - item['required_qty'])
                summary['materials'] += 1
                summary['shortages'] += int(item['shortage'] > 0)
            for item in template['structured_items']:
                item['required_qty'] *= plan['qty']
                if not item['is_semi']:
                    pn = item['child_part_no']
                    item['stock_qty'] = before.get(pn, 0)
                    item['shortage'] = max(0, item['required_qty'] - item['stock_qty'])
                    before[pn] = max(0, item['stock_qty'] - item['required_qty'])
            summary['missing_count'] += int(not row.has_bom)
            row.payload = {**plan, **template}
            row.ready = True
            processed.append(row)
        completed = job.completed + len(processed)
        with transaction.atomic():
            saved = BOMCalculationJob.objects.filter(pk=job.pk, revision=version, status='running').update(
                completed=completed, remaining=remaining, summary=summary, updated_at=timezone.now(),
                status='complete' if completed == job.total else 'pending')
            if saved:
                BOMCalculationRow.objects.bulk_update(processed, ['payload', 'ready'], batch_size=100)
    except Exception:
        logger.exception('BOM calculation chunk failed: %s', job.pk)
        BOMCalculationJob.objects.filter(pk=job.pk, revision=version, status='running').update(
            status='failed', error='계산 처리에 실패했습니다. 다시 시도하거나 관리자에게 작업 번호를 알려주세요.', updated_at=timezone.now())


def owned_job(request, key):
    try:
        return get_object_or_404(BOMCalculationJob, pk=key, owner=request.user)
    except (ValueError, ValidationError):
        from django.http import Http404
        raise Http404


class JobResults:
    """Repeatable streaming sequence for the existing exports/demand registration."""
    def __init__(self, job):
        self.job = job

    def __bool__(self):
        return self.job.status == 'complete' and self.job.total > 0

    def __len__(self):
        return self.job.total

    def __iter__(self):
        yield from self.job.rows.filter(ready=True).values_list('payload', flat=True).iterator(chunk_size=100)


def get_results(request, key):
    if str(key).startswith('job:'):
        job = owned_job(request, key[4:])
        return JobResults(job) if job.status == 'complete' else None
    return request.session.get(f'batch_calc_{key}')  # Existing open result tabs remain usable.


def job_context(request, job):
    result_url = reverse('material:bom_calculate') + '?job=' + str(job.pk)
    context = {'calc_type': 'batch', 'bom_job': job, 'session_key': f'job:{job.pk}',
        'batch_total': job.total, 'batch_metadata': job.summary, 'part_group_map': job.summary.get('part_groups', {}),
        'job_config': {'id': str(job.pk), 'step_url': reverse('material:bom_job_step', args=[job.pk]),
                       'result_url': result_url, 'complete': job.status == 'complete', 'total': job.total,
                       'completed': job.completed}}
    if job.status == 'complete':
        rows = job.rows.all()
        missing = request.GET.get('filter') == 'missing'
        if missing:
            rows = rows.filter(has_bom=False)
        try:
            per_page = int(request.GET.get('size', 50))
        except ValueError:
            per_page = 50
        per_page = per_page if per_page in (30, 50, 100) else 50
        page = Paginator(rows, per_page).get_page(request.GET.get('page', 1))
        context['job_config'].update(page=page.number, pages=page.paginator.num_pages,
                                     size=per_page, filter='missing' if missing else 'all')
        context.update(batch_results=[r.payload for r in page], batch_page=page, batch_filter='missing' if missing else 'all',
                       total_material_count=job.summary['materials'], total_shortage_count=job.summary['shortages'],
                       total_sufficient_count=job.summary['materials'] - job.summary['shortages'])
    return context


# Imported here to reuse the existing WMS permission policy.
from .views import wms_permission_required


@wms_permission_required('can_wms_bom_calc')
@require_POST
def start(request):
    upload = request.FILES.get('calc_file')
    if not upload:
        return JsonResponse({'error': '파일을 선택해주세요.'}, status=400)
    try:
        job = create_job(request.user, upload)
    except (ValueError, UnicodeError) as exc:
        return JsonResponse({'error': str(exc)}, status=400)
    except Exception:
        logger.exception('BOM upload failed')
        return JsonResponse({'error': '파일 분석에 실패했습니다. 파일 형식과 서버 상태를 확인해주세요.'}, status=400)
    # Remove old large session payloads only after a successful replacement upload.
    for key in list(request.session.keys()):
        if key.startswith('batch_calc_'):
            del request.session[key]
    return JsonResponse({'url': reverse('material:bom_calculate') + '?job=' + str(job.pk)})


@wms_permission_required('can_wms_bom_calc')
@require_POST
def step(request, job_id):
    job = owned_job(request, job_id)
    advance_job(job)
    job.refresh_from_db(fields=['status', 'completed', 'total', 'error'])
    return JsonResponse({'status': job.status, 'completed': job.completed, 'total': job.total, 'error': job.error})
