import os
import sys
import time
import logging
import threading

from django.apps import AppConfig

logger = logging.getLogger(__name__)

# 모듈 레벨 락 (스레드 간 동기화)
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
        """백그라운드에서 주기적으로 ERP 동기화 실행"""
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
                        from material.erp_api import (
                            sync_erp_incoming, sync_erp_issue, sync_erp_receipt,
                            sync_erp_stock_transfer, sync_erp_adjustments, sync_erp_outgoing,
                            sync_stock_from_erp,
                        )
                        from django.utils import timezone

                        # ── 1단계: 이력 동기화 (6개 트랜잭션, 재고 미반영) ──
                        sync_jobs = [
                            ('incoming', '입고', sync_erp_incoming),
                            ('issue', '생산출고', sync_erp_issue),
                            ('receipt', '생산입고', sync_erp_receipt),
                            ('transfer', '재고이동', sync_erp_stock_transfer),
                            ('adjust', '재고조정', sync_erp_adjustments),
                            ('outgoing', '고객출고', sync_erp_outgoing),
                        ]

                        for key, label, func in sync_jobs:
                            synced, skipped, errors, error_list = func()
                            cache.set(f'erp_{key}_sync_result', {
                                'synced': synced,
                                'skipped': skipped,
                                'errors': errors,
                                'error_list': error_list[:5],
                                'finished_at': timezone.now().strftime('%Y-%m-%d %H:%M'),
                                'auto': True,
                            }, timeout=86400)
                            if synced > 0:
                                logger.info(f'ERP {label} 자동 동기화: 신규 {synced}건, 건너뜀 {skipped}건, 오류 {errors}건')

                        # ── 2단계: 재고 동기화 (ERP 현재고로 SCM 총량 보정) ──
                        stock_result = sync_stock_from_erp()
                        if stock_result.get('adjusted', 0) > 0:
                            logger.info(
                                f'ERP 재고동기화: 조정 {stock_result["adjusted"]}건 '
                                f'(증가 {stock_result["increased"]}, 감소 {stock_result["decreased"]})'
                            )
                        cache.set('erp_stock_sync_result', {
                            'adjusted': stock_result.get('adjusted', 0),
                            'increased': stock_result.get('increased', 0),
                            'decreased': stock_result.get('decreased', 0),
                            'error': stock_result.get('error'),
                            'finished_at': timezone.now().strftime('%Y-%m-%d %H:%M'),
                            'auto': True,
                        }, timeout=86400)

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
