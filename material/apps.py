import os
import sys
import time
import logging
import threading

from django.apps import AppConfig

logger = logging.getLogger(__name__)

# 모듈 레벨 락 (스레드 간 동기화 동기화)
_sync_lock = threading.Lock()


class MaterialConfig(AppConfig):
    name = 'material'

    def ready(self):
        from django.conf import settings

        if not getattr(settings, 'ERP_ENABLED', False):
            return
        if not getattr(settings, 'ERP_AUTO_SYNC_ENABLED', False):
            return

        # runserver: reloader(RUN_MAIN=true) 프로세스에서만 실행
        if len(sys.argv) > 1 and sys.argv[1] == 'runserver':
            if os.environ.get('RUN_MAIN') != 'true':
                return
        elif len(sys.argv) > 0 and sys.argv[0].endswith('manage.py'):
            # migrate, collectstatic 등 다른 management command → 스킵
            return

        interval = getattr(settings, 'ERP_AUTO_SYNC_INTERVAL_MINUTES', 10) * 60

        thread = threading.Thread(target=self._run_scheduler, args=(interval,), daemon=True)
        thread.start()
        logger.info(f'ERP 자동 동기화 스케줄러 시작 ({interval // 60}분 간격)')

    @staticmethod
    def _run_scheduler(interval):
        """백그라운드에서 주기적으로 ERP 입고 동기화 실행"""
        from django.core.cache import cache

        # 서버 시작 후 첫 실행까지 60초 대기 (DB 연결 안정화)
        time.sleep(60)

        while True:
            try:
                # DB 커넥션 정리 (장시간 유휴 커넥션 타임아웃 방지)
                from django import db
                db.close_old_connections()

                # threading.Lock으로 수동 동기화와 충돌 방지
                acquired = _sync_lock.acquire(blocking=False)
                if not acquired:
                    logger.debug('ERP 자동 동기화 건너뜀: 다른 동기화 진행 중')
                else:
                    try:
                        from material.erp_api import sync_erp_incoming, sync_erp_issue, sync_erp_receipt, sync_erp_stock_transfer, sync_erp_adjustments, sync_erp_outgoing, adjust_stock_to_erp
                        from django.utils import timezone

                        # 구매입고 동기화
                        synced, skipped, errors, error_list = sync_erp_incoming()
                        cache.set('erp_incoming_sync_result', {
                            'synced': synced,
                            'skipped': skipped,
                            'errors': errors,
                            'error_list': error_list[:5],
                            'finished_at': timezone.now().strftime('%Y-%m-%d %H:%M'),
                            'auto': True,
                        }, timeout=86400)

                        if synced > 0:
                            logger.info(f'ERP 입고 자동 동기화: 신규 {synced}건, 건너뜀 {skipped}건, 오류 {errors}건')

                        # 생산출고 동기화
                        isu_synced, isu_skipped, isu_errors, isu_err_list = sync_erp_issue()
                        cache.set('erp_issue_sync_result', {
                            'synced': isu_synced,
                            'skipped': isu_skipped,
                            'errors': isu_errors,
                            'error_list': isu_err_list[:5],
                            'finished_at': timezone.now().strftime('%Y-%m-%d %H:%M'),
                            'auto': True,
                        }, timeout=86400)

                        if isu_synced > 0:
                            logger.info(f'ERP 생산출고 자동 동기화: 신규 {isu_synced}건, 건너뜀 {isu_skipped}건, 오류 {isu_errors}건')

                        # 생산입고 동기화
                        rcv_synced, rcv_skipped, rcv_errors, rcv_err_list = sync_erp_receipt()
                        cache.set('erp_receipt_sync_result', {
                            'synced': rcv_synced,
                            'skipped': rcv_skipped,
                            'errors': rcv_errors,
                            'error_list': rcv_err_list[:5],
                            'finished_at': timezone.now().strftime('%Y-%m-%d %H:%M'),
                            'auto': True,
                        }, timeout=86400)

                        if rcv_synced > 0:
                            logger.info(f'ERP 생산입고 자동 동기화: 신규 {rcv_synced}건, 건너뜀 {rcv_skipped}건, 오류 {rcv_errors}건')

                        # 재고이동 동기화
                        trf_synced, trf_skipped, trf_errors, trf_err_list = sync_erp_stock_transfer()
                        cache.set('erp_transfer_sync_result', {
                            'synced': trf_synced,
                            'skipped': trf_skipped,
                            'errors': trf_errors,
                            'error_list': trf_err_list[:5],
                            'finished_at': timezone.now().strftime('%Y-%m-%d %H:%M'),
                            'auto': True,
                        }, timeout=86400)

                        if trf_synced > 0:
                            logger.info(f'ERP 재고이동 자동 동기화: 신규 {trf_synced}건, 건너뜀 {trf_skipped}건, 오류 {trf_errors}건')

                        # 재고조정 동기화
                        adj_synced, adj_skipped, adj_errors, adj_err_list = sync_erp_adjustments()
                        cache.set('erp_adjust_sync_result', {
                            'synced': adj_synced,
                            'skipped': adj_skipped,
                            'errors': adj_errors,
                            'error_list': adj_err_list[:5],
                            'finished_at': timezone.now().strftime('%Y-%m-%d %H:%M'),
                            'auto': True,
                        }, timeout=86400)

                        if adj_synced > 0:
                            logger.info(f'ERP 재고조정 자동 동기화: 신규 {adj_synced}건, 건너뜀 {adj_skipped}건, 오류 {adj_errors}건')

                        # 고객출고 동기화
                        out_synced, out_skipped, out_errors, out_err_list = sync_erp_outgoing()
                        cache.set('erp_outgoing_sync_result', {
                            'synced': out_synced,
                            'skipped': out_skipped,
                            'errors': out_errors,
                            'error_list': out_err_list[:5],
                            'finished_at': timezone.now().strftime('%Y-%m-%d %H:%M'),
                            'auto': True,
                        }, timeout=86400)

                        if out_synced > 0:
                            logger.info(f'ERP 고객출고 자동 동기화: 신규 {out_synced}건, 건너뜀 {out_skipped}건, 오류 {out_errors}건')

                        # 매 사이클 마지막: ERP 현재고 기준 재고 보정
                        # (수불 동기화로 인한 이중 반영 방지 안전장치)
                        try:
                            adj_result = adjust_stock_to_erp()
                            if adj_result.get('adjusted', 0) > 0:
                                logger.info(
                                    f'ERP 재고 자동보정: {adj_result["adjusted"]}건 '
                                    f'(증가 {adj_result["increased"]}, 감소 {adj_result["decreased"]})'
                                )
                        except Exception as adj_e:
                            logger.error(f'ERP 재고 자동보정 오류: {adj_e}')

                    except Exception as e:
                        logger.error(f'ERP 자동 동기화 오류: {e}')
                        from django.utils import timezone
                        cache.set('erp_incoming_sync_result', {
                            'synced': 0, 'skipped': 0, 'errors': 1,
                            'error_list': [str(e)],
                            'finished_at': timezone.now().strftime('%Y-%m-%d %H:%M'),
                            'auto': True,
                        }, timeout=86400)
                    finally:
                        _sync_lock.release()

            except Exception as e:
                logger.error(f'ERP 자동 동기화 스케줄러 예외: {e}')

            time.sleep(interval)
