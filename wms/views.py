from datetime import datetime
from decimal import Decimal
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Max, Sum
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse

from orders.models import Part
from .forms import ReceiptCreateForm, StockUploadForm
from .models import WmsItemLookup, ErpStockSnapshot, WmsReceipt, WmsReceiptAttachment
from .utils import parse_erp_stock_file

def _is_quality_user(user) -> bool:
    if not user.is_authenticated:
        return False
    if user.is_staff or user.is_superuser:
        return True
    return user.groups.filter(name__in=["quality", "품질", "품질팀"]).exists()

@login_required
def stock_view(request):
    warehouse = request.GET.get("warehouse") or ""
    q = request.GET.get("q") or ""

    latest = ErpStockSnapshot.objects.aggregate(m=Max("snapshot_at"))["m"]
    warehouses = (
        ErpStockSnapshot.objects.filter(snapshot_at=latest)
        .values_list("warehouse_code", flat=True).distinct().order_by("warehouse_code")
        if latest else []
    )

    qs = ErpStockSnapshot.objects.filter(snapshot_at=latest) if latest else ErpStockSnapshot.objects.none()
    if warehouse:
        qs = qs.filter(warehouse_code=warehouse)
    if q:
        qs = qs.filter(part_no__icontains=q)

    rows = qs.order_by("warehouse_code", "part_no")[:2000]
    context = {
        "latest": latest,
        "warehouses": warehouses,
        "selected_warehouse": warehouse,
        "q": q,
        "rows": rows,
    }
    return render(request, "wms/stock.html", context)

@login_required
def receipt_list(request):
    status = request.GET.get("status") or ""
    qs = WmsReceipt.objects.all()
    if status:
        qs = qs.filter(status=status)
    return render(request, "wms/receipt_list.html", {"rows": qs[:500], "status": status})

@login_required
def receipt_create(request):
    if request.method == "POST":
        form = ReceiptCreateForm(request.POST)
        if form.is_valid():
            part_no = form.cleaned_data["part_no"].strip()
            lookup = WmsItemLookup.objects.filter(part_no=part_no).first()
            if not lookup:
                messages.error(request, "품번이 품목마스터(조회용)에 없습니다. 설정 > 품목 동기화를 먼저 수행해주세요.")
                return render(request, "wms/receipt_create.html", {"form": form})

            rec = WmsReceipt.objects.create(
                warehouse_code=form.cleaned_data["warehouse_code"].strip(),
                part_no=part_no,
                part_name=lookup.part_name,
                receipt_qty=form.cleaned_data["receipt_qty"],
                receipt_date=form.cleaned_data["receipt_date"],
                mfg_date=form.cleaned_data.get("mfg_date"),
                lot_no=form.cleaned_data.get("lot_no", "").strip(),
                status=WmsReceipt.STATUS_DRAFT,
                created_by=request.user,
            )
            messages.success(request, f"입고 등록 완료(임시저장): #{rec.id}")
            return redirect("wms:receipt_detail", receipt_id=rec.id)
    else:
        form = ReceiptCreateForm(initial={"receipt_date": datetime.today().date()})
    return render(request, "wms/receipt_create.html", {"form": form})

@login_required
def receipt_detail(request, receipt_id: int):
    rec = get_object_or_404(WmsReceipt, id=receipt_id)
    if request.method == "POST" and "request_qc" in request.POST:
        if rec.status not in [WmsReceipt.STATUS_DRAFT, WmsReceipt.STATUS_REJECTED]:
            messages.error(request, "현재 상태에서는 검사의뢰가 불가능합니다.")
            return redirect("wms:receipt_detail", receipt_id=rec.id)
        rec.status = WmsReceipt.STATUS_REQUESTED
        rec.requested_by = request.user
        rec.requested_at = datetime.now()
        rec.save(update_fields=["status", "requested_by", "requested_at", "updated_at"])
        messages.success(request, "수입검사 의뢰가 등록되었습니다. 품질팀 승인 대기 상태입니다.")
        return redirect("wms:receipt_detail", receipt_id=rec.id)

    if request.method == "POST" and "upload_file" in request.POST:
        if not _is_quality_user(request.user):
            return HttpResponseForbidden("권한이 없습니다.")
        f = request.FILES.get("file")
        if not f:
            messages.error(request, "첨부 파일이 없습니다.")
            return redirect("wms:receipt_detail", receipt_id=rec.id)
        WmsReceiptAttachment.objects.create(
            receipt=rec,
            file=f,
            original_name=f.name,
            uploaded_by=request.user,
        )
        messages.success(request, "성적서(첨부)가 등록되었습니다.")
        return redirect("wms:receipt_detail", receipt_id=rec.id)

    return render(request, "wms/receipt_detail.html", {"rec": rec})

@login_required
def quality_queue(request):
    if not _is_quality_user(request.user):
        return HttpResponseForbidden("권한이 없습니다.")
    qs = WmsReceipt.objects.filter(status=WmsReceipt.STATUS_REQUESTED).order_by("-requested_at")
    return render(request, "wms/quality_queue.html", {"rows": qs[:500]})

@login_required
def quality_approve(request, receipt_id: int):
    if not _is_quality_user(request.user):
        return HttpResponseForbidden("권한이 없습니다.")
    rec = get_object_or_404(WmsReceipt, id=receipt_id)
    if rec.status != WmsReceipt.STATUS_REQUESTED:
        messages.error(request, "검사의뢰 상태에서만 승인할 수 있습니다.")
        return redirect("wms:receipt_detail", receipt_id=rec.id)
    rec.status = WmsReceipt.STATUS_APPROVED
    rec.approved_by = request.user
    rec.approved_at = datetime.now()
    rec.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    messages.success(request, "승인 완료: 입고완료 처리되었습니다.")
    return redirect("wms:receipt_detail", receipt_id=rec.id)

@login_required
def quality_reject(request, receipt_id: int):
    if not _is_quality_user(request.user):
        return HttpResponseForbidden("권한이 없습니다.")
    rec = get_object_or_404(WmsReceipt, id=receipt_id)
    if request.method == "POST":
        reason = (request.POST.get("reason") or "").strip()
        if not reason:
            messages.error(request, "반려 사유를 입력해주세요.")
            return redirect("wms:quality_reject", receipt_id=rec.id)
        rec.status = WmsReceipt.STATUS_REJECTED
        rec.rejected_by = request.user
        rec.rejected_at = datetime.now()
        rec.reject_reason = reason
        rec.save(update_fields=["status", "rejected_by", "rejected_at", "reject_reason", "updated_at"])
        messages.success(request, "반려 처리되었습니다.")
        return redirect("wms:receipt_detail", receipt_id=rec.id)
    return render(request, "wms/quality_reject.html", {"rec": rec})

@login_required
def settings_page(request):
    return render(request, "wms/settings.html", {})

@login_required
def sync_items(request):
    if not request.user.is_staff and not request.user.is_superuser:
        return HttpResponseForbidden("권한이 없습니다.")
    # build distinct part_no -> part_name (first seen)
    parts = Part.objects.values_list("part_no", "part_name").order_by("part_no")
    count_new = 0
    count_updated = 0
    seen = {}
    for pno, pname in parts:
        if not pno:
            continue
        pno = str(pno).strip()
        pname = str(pname or "").strip()
        if not pname:
            continue
        if pno in seen:
            continue
        seen[pno] = pname

    for pno, pname in seen.items():
        obj, created = WmsItemLookup.objects.update_or_create(
            part_no=pno,
            defaults={"part_name": pname},
        )
        if created:
            count_new += 1
        else:
            count_updated += 1

    messages.success(request, f"품목 동기화 완료: 신규 {count_new} / 갱신 {count_updated}")
    return redirect("wms:settings")

@login_required
def autocomplete_item(request):
    q = (request.GET.get("q") or "").strip()
    items = WmsItemLookup.objects.filter(part_no__icontains=q).order_by("part_no")[:20] if q else []
    return JsonResponse({"items": [{"part_no": i.part_no, "part_name": i.part_name} for i in items]})

@login_required
def upload_stock_snapshot(request):
    if not request.user.is_staff and not request.user.is_superuser:
        return HttpResponseForbidden("권한이 없습니다.")
    if request.method == "POST":
        form = StockUploadForm(request.POST, request.FILES)
        if form.is_valid():
            f = form.cleaned_data["file"]
            snapshot_at = form.cleaned_data.get("snapshot_at") or datetime.now()
            batch_id = str(uuid.uuid4())
            try:
                rows = parse_erp_stock_file(f.name, f.read())
            except Exception as e:
                messages.error(request, f"파일 파싱 실패: {e}")
                return render(request, "wms/upload_stock.html", {"form": form})

            created = 0
            for r in rows:
                ErpStockSnapshot.objects.create(
                    batch_id=batch_id,
                    snapshot_at=snapshot_at,
                    warehouse_code=r["warehouse_code"],
                    part_no=r["part_no"],
                    qty_onhand=r["qty_onhand"],
                    source_file_name=f.name,
                    uploaded_by=request.user,
                )
                created += 1
            messages.success(request, f"재고 스냅샷 업로드 완료: {created}건 (기준일시 {snapshot_at})")
            return redirect("wms:stock")
    else:
        form = StockUploadForm()
    return render(request, "wms/upload_stock.html", {"form": form})
