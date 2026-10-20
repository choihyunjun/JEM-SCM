"""ERP current stock is authoritative, independently of ledger import failures."""
from django.core.cache import cache
from django.utils import timezone

from .erp_sync_lock import serialized_erp_sync
from .models import StockSyncRun


def stock_failed(stock):
    """No verified ERP balance was applied; a result without status counts as failed."""
    if not stock:
        return True
    return stock.get('status', 'failed' if stock.get('error') else 'success') == 'failed'


def stock_result_message(stock):
    """Applied-with-warnings must not read as a failed correction (and vice versa)."""
    if stock_failed(stock):
        return 'ERP 현재고 보정 실패(미적용).'
    if stock.get('error'):
        return f"ERP 현재고 보정 {stock.get('adjusted', 0)}건 적용, 일부 항목 확인 필요."
    return f"ERP 현재고 보정 완료 ({stock.get('adjusted', 0)}건)."


def record_sync_failure(message, summary=None, status='failed'):
    StockSyncRun.objects.create(status=status, finished_at=timezone.now(),
                                error=message, summary=summary or {})
    cache.set('erp_sync_progress', {'stage': '확인 필요', 'percent': 100,
                                    'error': message, 'detail': message}, 600)


@serialized_erp_sync
def sync_inventory(date_from=None, date_to=None):
    from . import erp_api
    jobs = [
        ('incoming', '구매입고', erp_api.sync_erp_incoming),
        ('issue', '생산출고', erp_api.sync_erp_issue),
        ('receipt', '생산입고', erp_api.sync_erp_receipt),
        ('transfer', '재고이동', erp_api.sync_erp_stock_transfer),
        ('adjust', '재고조정', erp_api.sync_erp_adjustments),
        ('outgoing', '고객출고', erp_api.sync_erp_outgoing),
    ]
    result = {'synced': 0, 'skipped': 0, 'errors': 0, 'error_list': [], 'stock': None,
              'steps': [], 'date_from': date_from, 'date_to': date_to}
    for index, (key, label, func) in enumerate(jobs):
        cache.set('erp_sync_progress', {'stage': f'{label} 동기화 중', 'percent': index * 12}, 600)
        try:
            dates = dict(date_from=date_from, date_to=date_to) if date_from or date_to else {}
            synced, skipped, errors, error_list = func(**dates)
        except Exception as exc:
            synced, skipped, errors, error_list = 0, 0, 1, [str(exc)]
        cache.set(f'erp_{key}_sync_result', {
            'synced': synced, 'skipped': skipped, 'errors': errors,
            'error_list': error_list[:5], 'finished_at': timezone.localtime().strftime('%Y-%m-%d %H:%M'),
        }, 86400)
        result['synced'] += synced
        result['skipped'] += skipped
        result['errors'] += errors
        result['error_list'].extend(f'{label}: {err}' for err in error_list)
        result['steps'].append({'stage': key, 'label': label, 'synced': synced,
                                'skipped': skipped, 'errors': errors, 'error_list': list(error_list)})
    result['ledger_errors'] = result['errors']
    try:
        result['stock'] = erp_api.sync_stock_from_erp()
    except Exception as exc:
        result['stock'] = {'adjusted': 0, 'error': str(exc), 'status': 'failed'}
    stock_error = result['stock'].get('error')
    if stock_error:
        result['errors'] += 1
        result['error_list'].append(f'ERP 현재고 보정: {stock_error}')
    message = None
    if result['errors']:
        message = stock_result_message(result['stock'])
        message += f' 수불 이력 오류 {result["ledger_errors"]}건. ' + '; '.join(result['error_list'][:5])
        record_sync_failure(message, result, status='failed' if stock_failed(result['stock']) else 'partial')
    cache.set('erp_sync_progress', {
        'stage': '확인 필요' if result['errors'] else '수불·총량 보정 완료',
        'percent': 100, 'error': message,
        'detail': message or f'수불 {result["synced"]}건, 재고 조정 {result["stock"]["adjusted"]}건',
    }, 600)
    return result
