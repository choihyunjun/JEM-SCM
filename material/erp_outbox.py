"""Commit local work first; persist uncertain ERP outcomes instead of blind retries."""
import logging
from functools import partial

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import ERPIncomingOperation as Operation, ERPIncomingEvent, MaterialTransaction

logger = logging.getLogger(__name__)


def _schedule(job):
    if getattr(settings, 'ERP_OUTBOX_AUTODISPATCH', True):
        transaction.on_commit(partial(dispatch, job.pk), robust=True)


def enqueue_registration(trx, body):
    with transaction.atomic():
        # A write locks the receipt on SQLite as well as row-locking databases.
        MaterialTransaction.objects.filter(pk=trx.pk).update(erp_sync_status=F('erp_sync_status'))
        trx.refresh_from_db(fields=['erp_incoming_no', 'erp_sync_status'])
        if trx.erp_incoming_no:
            return True, trx.erp_incoming_no, None
        existing = Operation.objects.filter(transaction=trx, kind='REGISTER').exclude(
            status__in=['SUCCESS', 'CANCELLED']).first()
        if existing:
            if existing.status in ('REVIEW', 'RUNNING'):
                return False, None, 'ERP 처리 여부 확인이 필요한 요청입니다. ERP 연동 관리에서 확인해 주세요.'
            return False, None, 'ERP 전송 대기 중입니다. ERP 연동 관리에서 상태를 확인해 주세요.'
        dependency = Operation.objects.filter(transaction_no=trx.transaction_no, kind='DELETE').exclude(
            status='SUCCESS').order_by('-created_at').first()
        job = Operation(kind='REGISTER', transaction=trx, transaction_no=trx.transaction_no,
                        payload=body, depends_on=dependency)
        marker = f'[SCM:{job.pk.hex}]'
        body['remarkDc'] = f"{body.get('remarkDc', '')} {marker}".strip()
        for detail in body.get('detail', []):
            detail['remarkDc'] = f"{detail.get('remarkDc', '')} {marker}".strip()
        job.save()
        ERPIncomingEvent.objects.create(operation=job, action='QUEUED', actor=trx.actor)
        MaterialTransaction.objects.filter(pk=trx.pk).update(
            erp_sync_status='PENDING', erp_sync_message='ERP 전송 대기')
        _schedule(job)
    job.refresh_from_db()
    if job.status == 'SUCCESS':
        trx.refresh_from_db(fields=['erp_incoming_no', 'erp_sync_status', 'erp_sync_message'])
        return True, job.erp_no, None
    # Existing callers already show nonempty errors as messages. Do not report queued work as complete.
    return False, None, 'ERP 전송 대기 또는 확인 필요: ERP 연동 관리에서 상태를 확인해 주세요.'


def enqueue_deletion(erp_no):
    with transaction.atomic():
        trx = MaterialTransaction.objects.filter(erp_incoming_no=erp_no).first()
        job, created = Operation.objects.get_or_create(
            kind='DELETE', erp_no=erp_no,
            defaults={'transaction': trx, 'transaction_no': trx.transaction_no if trx else '',
                      'payload': {'coCd': settings.ERP_COMPANY_CODE, 'rcvNb': erp_no}},
        )
        if job.status in ('RUNNING', 'REVIEW'):
            return False, 'ERP 삭제 처리 여부를 먼저 확인해 주세요.'
        if job.status != 'SUCCESS':
            if created:
                ERPIncomingEvent.objects.create(operation=job, action='QUEUED')
            _schedule(job)
    return True, None


def guard_receipt_change(trx):
    """Called within the same transaction as cancel/edit. Never erase uncertain work."""
    MaterialTransaction.objects.filter(pk=trx.pk).update(erp_sync_status=F('erp_sync_status'))
    trx.refresh_from_db(fields=['erp_incoming_no', 'erp_sync_status', 'erp_sync_message'])
    jobs = Operation.objects.filter(transaction=trx, kind='REGISTER')
    if jobs.filter(status__in=['RUNNING', 'REVIEW']).exists():
        raise ValueError('ERP 전송 결과를 먼저 확인해야 입고를 변경할 수 있습니다. ERP 연동 관리를 확인해 주세요.')
    return jobs.filter(status__in=['PENDING', 'FAILED']).update(status='CANCELLED', updated_at=timezone.now())


def _finish(job, status, message='', erp_no=''):
    with transaction.atomic():
        if job.transaction_id:
            MaterialTransaction.objects.filter(pk=job.transaction_id).update(erp_sync_status=F('erp_sync_status'))
        Operation.objects.filter(pk=job.pk).update(
            status=status, message=message, erp_no=erp_no or job.erp_no, updated_at=timezone.now())
        ERPIncomingEvent.objects.create(operation=job, action=status, message=message)
        if job.kind == 'REGISTER' and job.transaction_id:
            values = {'erp_sync_status': 'SUCCESS' if status == 'SUCCESS' else 'FAILED',
                      'erp_sync_message': message[:200]}
            if status == 'SUCCESS':
                values['erp_incoming_no'] = erp_no
            MaterialTransaction.objects.filter(pk=job.transaction_id).update(**values)
        elif job.kind == 'DELETE' and status == 'SUCCESS':
            MaterialTransaction.objects.filter(erp_incoming_no=job.erp_no).update(
                erp_incoming_no=None, erp_sync_status='PENDING', erp_sync_message='ERP 삭제 완료')


def dispatch(job_id):
    """Only unsent requests are dispatchable. A crash leaves RUNNING for reconciliation."""
    from .erp_api import call_erp_api
    if not getattr(settings, 'ERP_ENABLED', False):
        return
    receipt_id = Operation.objects.filter(pk=job_id).values_list('transaction_id', flat=True).first()
    with transaction.atomic():
        if receipt_id:
            MaterialTransaction.objects.filter(pk=receipt_id).update(erp_sync_status=F('erp_sync_status'))
        if Operation.objects.filter(pk=job_id, status='PENDING').update(
            status='RUNNING', attempts=F('attempts') + 1, updated_at=timezone.now()
        ) != 1:
            return
        job = Operation.objects.select_related('depends_on').get(pk=job_id)
        if job.depends_on and job.depends_on.status != 'SUCCESS':
            Operation.objects.filter(pk=job_id).update(status='PENDING', attempts=F('attempts') - 1)
            return
        if job.kind == 'REGISTER' and not job.transaction_id:
            Operation.objects.filter(pk=job_id).update(status='CANCELLED')
            return
        ERPIncomingEvent.objects.create(operation=job, action='DISPATCH')
    # No stock transaction is open while talking to ERP.
    endpoint = '/apiproxy/api20A02I00201' if job.kind == 'REGISTER' else '/apiproxy/api20A02D00201'
    try:
        success, data, error = call_erp_api(endpoint, job.payload)
        erp_no = data.get('resultData') if success and isinstance(data, dict) else None
        if success and (job.kind == 'DELETE' or isinstance(erp_no, str) and erp_no.strip()):
            _finish(job, 'SUCCESS', 'ERP 처리 완료', erp_no if job.kind == 'REGISTER' else job.erp_no)
        else:
            _finish(job, 'REVIEW', error or 'ERP 처리 결과가 불명확합니다. 전표를 확인해 주세요.')
    except Exception:
        # Including a DB failure after ERP success: preserve RUNNING if even this write fails.
        logger.exception('ERP outbox dispatch failed: %s', job.pk)
        _finish(job, 'REVIEW', '통신 또는 결과 저장 실패. 재전송 전에 ERP 전표를 확인해 주세요.')


def reconcile(job_id):
    """Read-only ERP lookup; absence alone never authorizes another registration."""
    from .erp_api import fetch_erp_incoming_headers, fetch_erp_incoming_detail
    job = Operation.objects.get(pk=job_id)
    if job.status == 'SUCCESS':
        return True, '이미 완료된 요청입니다.'
    if job.status not in ('REVIEW', 'RUNNING'):
        return False, '확인이 필요한 요청이 아닙니다.'
    # Avoid racing a currently executing call. Requests time out after 30 seconds.
    if job.status == 'RUNNING' and (timezone.now() - job.updated_at).total_seconds() < 120:
        return False, '전송 중입니다. 잠시 후 다시 확인해 주세요.'
    if job.status == 'RUNNING':
        _finish(job, 'REVIEW', '중단된 전송의 ERP 처리 여부를 확인해야 합니다.')
    if job.kind == 'DELETE':
        ok, rows, error = fetch_erp_incoming_detail(job.erp_no)
        if ok and isinstance(rows, list) and not rows:
            _finish(job, 'SUCCESS', 'ERP 조회로 삭제 완료 확인', job.erp_no)
            return True, '삭제 완료를 확인했습니다.'
        return False, error or '삭제 완료를 확인하지 못했습니다. ERP에서 전표를 확인해 주세요.'
    dt = job.payload['keyDt']
    ok, headers, error = fetch_erp_incoming_headers(dt, dt)
    if not ok or not isinstance(headers, list):
        return False, error or 'ERP 조회에 실패했습니다.'
    marker = f'[SCM:{job.pk.hex}]'
    found = []
    for header in headers:
        no = header.get('rcvNb')
        if not no:
            continue
        ok, rows, error = fetch_erp_incoming_detail(no)
        if not ok or not isinstance(rows, list):
            return False, error or 'ERP 상세 조회가 불완전합니다.'
        if marker in str(header.get('remarkDc', '')) or any(marker in str(r.get('remarkDc', '')) for r in rows):
            detail = job.payload['detail'][0]
            if (len(rows) == 1 and rows[0].get('itemCd') == detail['itemCd']
                    and str(rows[0].get('rcvQt')) == str(detail['rcvQt'])):
                found.append(no)
    if len(found) == 1:
        _finish(job, 'SUCCESS', 'ERP 조회로 등록 완료 확인', found[0])
        return True, '등록 완료를 확인했습니다.'
    return False, '일치하는 전표를 확정하지 못했습니다. 임의로 재전송하지 않았습니다.'


def retry_after_verification(job_id, actor, note):
    """Operator records an ERP-side check; never automatically retry an uncertain send."""
    if not note.strip():
        raise ValueError('ERP에서 미처리 상태를 확인한 내용을 입력해 주세요.')
    with transaction.atomic():
        if Operation.objects.filter(pk=job_id, status__in=['REVIEW', 'FAILED']).update(
            status='PENDING', updated_at=timezone.now()
        ) != 1:
            raise ValueError('전송 중이거나 이미 완료된 요청은 재전송할 수 없습니다.')
        job = Operation.objects.get(pk=job_id)
        ERPIncomingEvent.objects.create(operation=job, action='VERIFIED_RETRY', actor=actor, message=note)
        _schedule(job)
