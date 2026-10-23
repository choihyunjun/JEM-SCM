"""라벨 일괄 재발행 (전환용): 현재 실재고 LOT대로 새 라벨을 만들고, 그 품목·창고의 기존 라벨은 폐기한다.

새 라벨 QR에는 라벨 번호만 들어간다. 포대 품목은 단위중량(품목 설정)마다 1장, 파렛트 품목은 LOT마다 1장.
"""
import math
from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Sum
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from orders.decorators import wms_permission_required
from orders.models import Part
from .labels import LIVE
from .models import MaterialStock, RawMaterialLabel, RawMaterialSetting, Warehouse

WAREHOUSES = ('3200', '3000', '4300')


def _bags(qty, unit):
    """수량을 포대 단위로 나눈다 (마지막 포대는 남은 양)."""
    full = int(qty // unit)
    sizes = [unit] * full
    if qty - unit * full > 0:
        sizes.append(qty - unit * full)
    return sizes


def _new_labels(part, warehouse, lot_no, qty, setting, previous, user):
    pallet = setting is not None and setting.label_unit == 'PALLET'
    unit = setting.unit_weight if setting and setting.unit_weight and setting.unit_weight > 0 else Decimal(25)
    expiry = (lot_no + timedelta(days=setting.shelf_life_days)) if setting else (previous.expiry_date if previous else None)
    labels = []
    for size in ([qty] if pallet else _bags(qty, unit)):
        labels.append(RawMaterialLabel.objects.create(
            label_id=RawMaterialLabel.generate_pallet_label_id() if pallet else RawMaterialLabel.generate_label_id(),
            label_type='PALLET' if pallet else 'PACKAGE', part=part, part_no=part.part_no, part_name=part.part_name,
            lot_no=lot_no, quantity=size, unit=previous.unit if previous else 'KG', expiry_date=expiry,
            vendor=previous.vendor if previous else None, status='INSTOCK', warehouse=warehouse, printed_by=user,
        ))
    return labels


@wms_permission_required('can_wms_incoming_label')
def label_reissue(request):
    code = request.POST.get('warehouse') or request.GET.get('warehouse') or '3200'
    warehouse = Warehouse.objects.filter(code=code).first()
    if request.method == 'POST' and warehouse:
        part_ids = [int(p) for p in request.POST.getlist('part_ids') if p.isdigit()]
        made = {'PACKAGE': [], 'PALLET': []}
        disposed = 0
        with transaction.atomic():
            for part in Part.objects.filter(pk__in=part_ids):
                setting = RawMaterialSetting.objects.filter(part=part).first()
                old = RawMaterialLabel.objects.filter(part=part, warehouse=warehouse, status__in=LIVE)
                disposed += old.update(status='DISPOSED')
                for row in (MaterialStock.objects.filter(warehouse=warehouse, part=part, lot_no__isnull=False,
                                                         quantity__gt=0).values('lot_no').annotate(q=Sum('quantity'))):
                    previous = RawMaterialLabel.objects.filter(part=part, lot_no=row['lot_no']).order_by('-pk').first()
                    for label in _new_labels(part, warehouse, row['lot_no'], row['q'], setting, previous, request.user):
                        made[label.label_type].append(label.pk)
        messages.success(request, f"{warehouse.code} 창고: 새 라벨 {len(made['PACKAGE']) + len(made['PALLET'])}장 발행, "
                                  f"기존 라벨 {disposed}장 폐기 처리")
        # 새로고침으로 다시 발행되지 않게 결과는 주소로 넘긴다 (POST 후 이동)
        return redirect(f"{reverse('material:label_reissue')}?warehouse={warehouse.code}"
                        f"&bags={','.join(map(str, made['PACKAGE']))}&pallets={','.join(map(str, made['PALLET']))}")

    rows = []
    if warehouse:
        from .labels import raw_material_part_nos
        stock = (MaterialStock.objects.filter(warehouse=warehouse, lot_no__isnull=False, quantity__gt=0)
                 .values('part_id', 'part__part_no', 'part__part_name')
                 .annotate(q=Sum('quantity'), lots=Count('lot_no', distinct=True)).order_by('part__part_no'))
        raw = raw_material_part_nos(r['part__part_no'] for r in stock)
        live = dict(RawMaterialLabel.objects.filter(warehouse=warehouse, status__in=LIVE)
                    .values_list('part_id').annotate(n=Count('pk')))
        settings = {s.part_id: s for s in RawMaterialSetting.objects.filter(part_id__in=[r['part_id'] for r in stock])}
        for r in stock:
            if r['part__part_no'] not in raw:
                continue
            s = settings.get(r['part_id'])
            pallet = s is not None and s.label_unit == 'PALLET'
            unit = s.unit_weight if s and s.unit_weight and s.unit_weight > 0 else Decimal(25)
            rows.append({'part_id': r['part_id'], 'part_no': r['part__part_no'], 'part_name': r['part__part_name'],
                         'qty': r['q'], 'lots': r['lots'], 'live': live.get(r['part_id'], 0),
                         'unit': '파렛트' if pallet else f'포대 {unit.normalize():f}',
                         'new': r['lots'] if pallet else math.ceil(r['q'] / unit) if unit else 0})
    bags, pallets = request.GET.get('bags', ''), request.GET.get('pallets', '')
    return render(request, 'material/label_reissue.html', {
        'rows': rows, 'warehouse': warehouse, 'warehouses': Warehouse.objects.filter(code__in=WAREHOUSES),
        'bag_url': f"{reverse('material:raw_material_label_print')}?ids={bags}" if bags else '',
        'pallet_url': f"{reverse('material:pallet_label_print')}?ids={pallets}" if pallets else '',
        'bag_count': len([i for i in bags.split(',') if i]), 'pallet_count': len([i for i in pallets.split(',') if i]),
    })
