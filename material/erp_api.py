"""
더존 아마란스10 ERP API 연동 모듈
- 입고정보 등록/삭제, BOM 조회 등 ERP 연동 함수 제공
- HMAC-SHA256 인증 프로토콜
"""

import hmac
import hashlib
import base64
import time
import secrets
import logging
import requests

from django.conf import settings
from django.db import transaction

logger = logging.getLogger(__name__)


def _generate_trx_no():
    """
    수불번호 생성 (TRX-YYYYMMDD-NNNN)
    """
    from material.models import MaterialTransaction
    from django.utils import timezone as tz
    from django.db.models import Max

    today_str = tz.localtime(tz.now()).strftime('%Y%m%d')
    prefix = f'TRX-{today_str}'

    max_no = (
        MaterialTransaction.objects
        .filter(transaction_no__startswith=prefix)
        .aggregate(m=Max('transaction_no'))
    )['m']

    if max_no:
        try:
            seq = int(max_no.split('-')[-1]) + 1
        except (ValueError, IndexError):
            seq = 1
    else:
        seq = 1

    return f'{prefix}-{seq:04d}'


def _create_trx(**kwargs):
    """
    MaterialTransaction 생성 (채번 충돌 시 자동 재시도)
    transaction_no를 자동 생성하므로 kwargs에 포함하지 않아도 됨
    """
    from material.models import MaterialTransaction
    from django.db import IntegrityError

    for attempt in range(10):
        kwargs['transaction_no'] = _generate_trx_no()
        try:
            with transaction.atomic():
                return MaterialTransaction.objects.create(**kwargs)
        except IntegrityError:
            continue

    # 최종 폴백: uuid 기반 번호
    import uuid
    kwargs['transaction_no'] = f'TRX-{uuid.uuid4().hex[:12].upper()}'
    return MaterialTransaction.objects.create(**kwargs)


def _generate_sign(access_token, hash_key, transaction_id, timestamp, url):
    """wehago-sign 생성: HMAC-SHA256 + Base64"""
    value = access_token + transaction_id + timestamp + url
    sig = hmac.new(
        hash_key.encode('utf-8'),
        value.encode('utf-8'),
        hashlib.sha256
    ).digest()
    return base64.b64encode(sig).decode('utf-8')


def call_erp_api(url, body):
    """
    더존 아마란스 API 호출 (범용)
    Returns: (success: bool, data: dict or None, error: str or None)
    """
    if not getattr(settings, 'ERP_ENABLED', False):
        return False, None, 'ERP 연동 비활성화 상태'

    base_url = settings.ERP_API_BASE_URL
    access_token = settings.ERP_API_ACCESS_TOKEN
    hash_key = settings.ERP_API_HASH_KEY

    tid = secrets.token_hex(15)
    ts = str(int(time.time()))
    sign = _generate_sign(access_token, hash_key, tid, ts, url)

    headers = {
        'Content-Type': 'application/json',
        'callerName': settings.ERP_API_CALLER_NAME,
        'Authorization': f'Bearer {access_token}',
        'transaction-id': tid,
        'timestamp': ts,
        'groupSeq': settings.ERP_API_GROUP_SEQ,
        'wehago-sign': sign,
    }

    try:
        resp = requests.post(base_url + url, headers=headers, json=body, timeout=30)
        data = resp.json()

        if data.get('resultCode') == 0:
            return True, data, None
        else:
            error_msg = data.get('resultMsg', f'ERP 오류 (코드: {data.get("resultCode")})')
            logger.warning(f'ERP API 오류: {url} -> {error_msg}')
            return False, data, error_msg

    except requests.exceptions.Timeout:
        logger.error(f'ERP API 타임아웃: {url}')
        return False, None, 'ERP API 타임아웃 (30초 초과)'
    except requests.exceptions.ConnectionError:
        logger.error(f'ERP API 연결 실패: {url}')
        return False, None, 'ERP API 연결 실패'
    except Exception as e:
        logger.error(f'ERP API 예외: {url} -> {e}')
        return False, None, f'ERP API 오류: {str(e)}'


def register_erp_incoming(trx, qty, warehouse_code, erp_order_no='', erp_order_seq=''):
    """
    ERP 입고정보 등록
    - trx: MaterialTransaction 객체
    - qty: 입고수량 (양품수량)
    - warehouse_code: 입고창고 코드 (예: '2000')
    - erp_order_no: ERP 발주번호 (있으면 발주입고, 없으면 예외입고)
    - erp_order_seq: ERP 발주순번
    Returns: (success: bool, erp_no: str or None, error: str or None)
    """
    if not getattr(settings, 'ERP_ENABLED', False):
        return False, None, 'ERP 비활성화'

    # 거래처 ERP 코드 확인
    vendor = trx.vendor
    if not vendor or not vendor.erp_code:
        logger.info(f'ERP 입고등록 건너뜀: 거래처 ERP코드 없음 (trx={trx.transaction_no})')
        return False, None, None  # 에러가 아닌 건너뜀

    # LOT 번호 포맷
    lot_str = ''
    if trx.lot_no:
        if hasattr(trx.lot_no, 'strftime'):
            lot_str = trx.lot_no.strftime('%y%m%d') + '-001'
        else:
            lot_str = str(trx.lot_no).replace('-', '')[:6] + '-001'

    # 입고일자
    if hasattr(trx.date, 'strftime'):
        key_dt = trx.date.strftime('%Y%m%d')
    else:
        key_dt = str(trx.date).replace('-', '')[:8]

    body = {
        'coCd': settings.ERP_COMPANY_CODE,
        'trCd': vendor.erp_code,
        'keyFg': 'RV',
        'keyDt': key_dt,
        'whCd': warehouse_code,
        'poFg': '0',           # DOMESTIC
        'exchCd': 'KRW',
        'exchRt': 1,
        'lcYn': '0',
        'empCd': getattr(settings, 'ERP_DEFAULT_EMP_CODE', '20240601'),
        'deptCd': getattr(settings, 'ERP_DEFAULT_DEPT_CODE', '0630'),
        'divCd': settings.ERP_DIVISION_CODE,
        'vatFg': '0',          # 매입과세
        'mapFg': '1' if erp_order_no else '0',  # 1=발주입고, 0=예외입고
        'remarkDc': f'SCM 입고 ({trx.transaction_no})',
        'procFg': '1',         # 일괄
        'umvatFg': '0',        # 부가세미포함
        'detail': [{
            'rcvSq': 1,
            'itemCd': trx.part.part_no,
            'poQt': qty,
            'rcvQt': qty,
            'rcvUm': 0,
            'rcvgAm': 0,
            'rcvvAm': 0,
            'rcvhAm': 0,
            'exchCd': 'KRW',
            'exchRt': 1,
            'exchUm': 0,
            'exchAm': 0,
            'lotNb': lot_str,
            'umFg': '',
            'lcCd': warehouse_code,
            'remarkDc': f'SCM ({trx.transaction_no})',
            'vatUm': 0,
            **(  # 발주입고 시 발주번호/순번 포함
                {'poNb': erp_order_no, 'poSq': erp_order_seq}
                if erp_order_no else {}
            ),
        }]
    }

    success, data, error = call_erp_api('/apiproxy/api20A02I00201', body)

    # 결과를 트랜잭션에 저장
    if success:
        erp_no = data.get('resultData', '')
        trx.erp_incoming_no = erp_no
        trx.erp_sync_status = 'SUCCESS'
        trx.erp_sync_message = f'ERP 입고번호: {erp_no}'
        trx.save(update_fields=['erp_incoming_no', 'erp_sync_status', 'erp_sync_message'])
        logger.info(f'ERP 입고등록 성공: {trx.transaction_no} -> {erp_no}')
        return True, erp_no, None
    else:
        trx.erp_sync_status = 'FAILED'
        trx.erp_sync_message = error or 'Unknown error'
        trx.save(update_fields=['erp_sync_status', 'erp_sync_message'])
        logger.warning(f'ERP 입고등록 실패: {trx.transaction_no} -> {error}')
        return False, None, error


def delete_erp_incoming(erp_incoming_no):
    """
    ERP 입고정보 삭제
    - erp_incoming_no: ERP 입고번호 (예: 'RV2602000217')
    Returns: (success: bool, error: str or None)
    """
    if not getattr(settings, 'ERP_ENABLED', False):
        return False, 'ERP 비활성화'

    if not erp_incoming_no:
        return False, 'ERP 입고번호 없음'

    body = {
        'coCd': settings.ERP_COMPANY_CODE,
        'rcvNb': erp_incoming_no,
    }

    success, data, error = call_erp_api('/apiproxy/api20A02D00201', body)

    if success:
        logger.info(f'ERP 입고삭제 성공: {erp_incoming_no}')
        return True, None
    else:
        logger.warning(f'ERP 입고삭제 실패: {erp_incoming_no} -> {error}')
        return False, error


def fetch_erp_bom(parent_code):
    """
    ERP BOM 조회
    - parent_code: 모품코드 (예: '064133-0010')
    Returns: (success: bool, items: list or None, error: str or None)
    """
    if not getattr(settings, 'ERP_ENABLED', False):
        return False, None, 'ERP 비활성화'

    body = {
        'coCd': settings.ERP_COMPANY_CODE,
        'itemparentCd': parent_code,
        'useYn': '1',
    }

    success, data, error = call_erp_api('/apiproxy/api20A00S01001', body)

    if success:
        items = data.get('resultData', [])
        return True, items, None
    else:
        return False, None, error


def sync_single_bom(part_no):
    """
    단일 모품 BOM 동기화
    - ERP에서 해당 모품의 BOM 조회 후 DB 갱신
    - 기존 BOMItem 삭제 후 새로 생성
    Returns: (success: bool, item_count: int, error: str or None)
    """
    from material.models import Product, BOMItem
    from decimal import Decimal

    ok, items, err = fetch_erp_bom(part_no)
    if not ok:
        return False, 0, err or 'ERP 조회 실패'
    if not items:
        return False, 0, 'ERP에 BOM 데이터 없음'

    first = items[0]
    parent_acct = '반제품' if first.get('itemparentCd', '').endswith('A') else '제품'

    # Product 생성 또는 업데이트
    product, _ = Product.objects.update_or_create(
        part_no=part_no,
        defaults={
            'part_name': first.get('itemparentNm', part_no),
            'spec': first.get('itemparentDc', '') or '',
            'unit': first.get('itemparentUnitDc', 'EA'),
            'account_type': parent_acct,
            'is_bom_registered': True,
            'is_active': True,
        }
    )

    # 기존 BOMItem 삭제 후 새로 생성
    product.bom_items.all().delete()

    for item in items:
        child_code = item.get('itemchildCd', '')
        if not child_code:
            continue
        use_yn = item.get('useYn', '1') == '1'

        BOMItem.objects.create(
            product=product,
            seq=item.get('bomSq', 1),
            child_part_no=child_code,
            child_part_name=item.get('itemchildNm', ''),
            child_spec=item.get('itemchildDc', '') or '',
            child_unit=item.get('itemchildUnitDc', 'EA'),
            net_qty=Decimal(str(item.get('justQt', 0))),
            loss_rate=Decimal(str(item.get('lossRt', 0))),
            required_qty=Decimal(str(item.get('realQt', 0))),
            supply_type='사급' if item.get('bomOdrFg') == '1' else '자재',
            outsource_type='유상' if item.get('outFg') == '1' else '무상',
            vendor_name=item.get('attrNm', '') or '',
            drawing_no=item.get('designNb', '') or '',
            is_active=use_yn,
            is_bom_active=use_yn,
        )

    logger.info(f'단일 BOM 동기화 완료: {part_no} ({len(items)}개 자품)')
    return True, len(items), None


def sync_all_bom():
    """
    ERP에서 전체 BOM 데이터를 동기화 (클린 동기화)
    1. 기존 모품코드 목록 수집
    2. BOMItem + Product 전체 삭제
    3. 각 모품코드로 ERP 조회 → 새로 생성
    Returns: (synced_count, skipped_count, error_count, errors)
    """
    from material.models import Product, BOMItem
    from decimal import Decimal

    # 1) 기존 모품코드 목록 수집
    part_nos = list(Product.objects.values_list('part_no', flat=True))
    logger.info(f'BOM 동기화 시작: 기존 모품 {len(part_nos)}건 대상')

    # 2) 기존 데이터 전체 삭제
    BOMItem.objects.all().delete()
    Product.objects.all().delete()
    logger.info('기존 BOM 데이터 전체 삭제 완료')

    # 계정구분 매핑
    acct_map = {
        '0': '원재료', '1': '부재료', '2': '제품',
        '4': '부재료', '5': '상품', '6': '반제품', '7': '부산물', '8': '기타',
    }

    synced = 0
    skipped = 0
    errors = 0
    error_list = []

    # 3) 각 모품코드로 ERP 조회 → 새로 생성
    for part_no in part_nos:
        try:
            ok, items, err = fetch_erp_bom(part_no)

            if not ok or not items:
                skipped += 1
                continue

            first = items[0]

            # 모품(Product) 생성
            acct_fg = first.get('acctFg', '')
            # 모품 자체의 계정은 응답에 직접 없으므로, 반제품 여부는 모품코드에 'A'가 붙거나 acctFgNm으로 판단
            parent_acct = '반제품' if first.get('itemparentCd', '').endswith('A') else '제품'

            product = Product.objects.create(
                part_no=part_no,
                part_name=first.get('itemparentNm', part_no),
                spec=first.get('itemparentDc', '') or '',
                unit=first.get('itemparentUnitDc', 'EA'),
                account_type=parent_acct,
                procurement_type='생산',
                is_bom_registered=True,
                is_active=True,
            )

            # BOMItem 생성
            for item in items:
                child_code = item.get('itemchildCd', '')
                if not child_code:
                    continue

                use_yn = item.get('useYn', '1') == '1'

                BOMItem.objects.create(
                    product=product,
                    seq=item.get('bomSq', 1),
                    child_part_no=child_code,
                    child_part_name=item.get('itemchildNm', ''),
                    child_spec=item.get('itemchildDc', '') or '',
                    child_unit=item.get('itemchildUnitDc', 'EA'),
                    net_qty=Decimal(str(item.get('justQt', 0))),
                    loss_rate=Decimal(str(item.get('lossRt', 0))),
                    required_qty=Decimal(str(item.get('realQt', 0))),
                    supply_type='사급' if item.get('bomOdrFg') == '1' else '자재',
                    outsource_type='유상' if item.get('outFg') == '1' else '무상',
                    vendor_name=item.get('attrNm', '') or '',
                    drawing_no=item.get('designNb', '') or '',
                    is_active=use_yn,
                    is_bom_active=use_yn,
                )

            synced += 1

        except Exception as e:
            errors += 1
            error_list.append(f'{part_no}: {str(e)}')
            logger.error(f'BOM 동기화 오류 ({part_no}): {e}')

    logger.info(f'BOM 동기화 완료: 성공 {synced}, 건너뜀 {skipped}, 오류 {errors}')
    return synced, skipped, errors, error_list


# =============================================================================
# ERP 입고정보 조회 (역방향 동기화: ERP → WMS)
# =============================================================================

def fetch_erp_incoming_headers(date_from, date_to):
    """
    ERP 입고정보 헤더 내역 조회
    - date_from/date_to: 'YYYYMMDD' 형식 문자열
    Returns: (success, items_list, error)
    """
    if not getattr(settings, 'ERP_ENABLED', False):
        return False, None, 'ERP 비활성화'

    body = {
        'coCd': settings.ERP_COMPANY_CODE,
        'rcvDtFrom': date_from,
        'rcvDtTo': date_to,
    }

    success, data, error = call_erp_api('/apiproxy/api20A02S00201', body)

    if success:
        items = data.get('resultData', [])
        return True, items, None
    else:
        return False, None, error


def fetch_erp_incoming_detail(rcv_nb):
    """
    ERP 입고정보 디테일 내역 조회
    - rcv_nb: 입고번호 (예: 'RV2602000217')
    Returns: (success, items_list, error)
    """
    if not getattr(settings, 'ERP_ENABLED', False):
        return False, None, 'ERP 비활성화'

    body = {
        'coCd': settings.ERP_COMPANY_CODE,
        'rcvNb': rcv_nb,
    }

    success, data, error = call_erp_api('/apiproxy/api20A02S00202', body)

    if success:
        items = data.get('resultData', [])
        return True, items, None
    else:
        return False, None, error


def sync_erp_incoming(date_from=None, date_to=None):
    """
    ERP 입고 내역을 WMS에 동기화 (역방향)
    - ERP에서 직접 처리된 입고건을 WMS에 IN_ERP로 생성
    - WMS에서 올린 건(remarkDc에 "SCM" 포함)은 제외
    - 이미 동기화된 건(erp_incoming_no 매칭)은 건너뜀
    Returns: (synced_count, skipped_count, error_count, error_list)
    """
    from material.models import MaterialTransaction, MaterialStock, Warehouse
    from orders.models import Part, Vendor
    from django.db import models
    from django.utils import timezone as tz
    from django.core.cache import cache
    from datetime import datetime, timedelta

    if date_from is None:
        # 기초재고 기준일이 있으면 그 날짜부터, 없으면 어제부터
        cutoff = cache.get('erp_stock_init_date') or getattr(settings, 'ERP_STOCK_INIT_DATE', '')
        if cutoff:
            date_from = cutoff
            logger.info(f'기초재고 기준일 적용: {cutoff} 이후 건만 동기화')
        else:
            yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y%m%d')
            date_from = yesterday
    if date_to is None:
        date_to = datetime.now().strftime('%Y%m%d')

    logger.info(f'ERP 입고 동기화 시작: {date_from} ~ {date_to}')

    # 1) 헤더 조회
    ok, headers, err = fetch_erp_incoming_headers(date_from, date_to)
    if not ok:
        logger.error(f'ERP 입고 헤더 조회 실패: {err}')
        return 0, 0, 1, [f'헤더 조회 실패: {err}']
    if not headers:
        logger.info('ERP 입고 내역 없음')
        return 0, 0, 0, []

    # 2) 이미 동기화된 입고번호 목록
    existing_rcv_nbs = set(
        MaterialTransaction.objects.filter(
            erp_incoming_no__isnull=False
        ).values_list('erp_incoming_no', flat=True)
    )

    synced = 0
    skipped = 0
    errors = 0
    error_list = []

    # 기본 창고 (매칭 실패 시)
    default_wh = Warehouse.objects.filter(code='2000').first()

    for header in headers:
        rcv_nb = header.get('rcvNb', '')
        if not rcv_nb:
            continue

        # SCM에서 올린 건 제외
        remark = header.get('remarkDc', '') or ''
        if 'SCM' in remark:
            skipped += 1
            continue

        # 이미 동기화된 건 제외 (existing_nbs에는 'RV...-순번' 형태이므로 prefix로 체크)
        if any(nb.startswith(rcv_nb) for nb in existing_rcv_nbs):
            skipped += 1
            continue

        try:
            # 3) 디테일 조회
            ok2, details, err2 = fetch_erp_incoming_detail(rcv_nb)
            if not ok2 or not details:
                skipped += 1
                continue

            # 헤더 정보
            rcv_dt = header.get('rcvDt', '')  # 'YYYYMMDD'
            tr_cd = header.get('trCd', '')
            vendor_name = header.get('attrNm', '')
            wh_cd = header.get('whCd', '')

            # Vendor 매칭
            vendor = None
            if tr_cd:
                vendor = Vendor.objects.filter(erp_code=tr_cd).first()

            # Warehouse 매칭
            warehouse = None
            if wh_cd:
                warehouse = Warehouse.objects.filter(code=wh_cd).first()
            if not warehouse:
                warehouse = default_wh

            # 입고일 파싱 (ERP는 날짜만 제공 → 동기화 시점 시간 조합)
            try:
                erp_date = datetime.strptime(rcv_dt, '%Y%m%d').date()
                now = tz.localtime(tz.now())
                rcv_date = now.replace(year=erp_date.year, month=erp_date.month, day=erp_date.day)
            except (ValueError, TypeError):
                rcv_date = tz.now()

            # 4) 각 디테일(품목)별로 트랜잭션 생성
            for detail in details:
                item_cd = detail.get('itemCd', '')
                if not item_cd:
                    continue

                rcv_sq = detail.get('rcvSq', 1)
                # 중복 체크 (입고번호 + 순번)
                trx_key = f'{rcv_nb}-{rcv_sq}'
                if trx_key in existing_rcv_nbs:
                    continue

                # Part 매칭
                part = Part.objects.filter(part_no=item_cd).first()
                if not part:
                    logger.warning(f'ERP 입고 동기화: 품번 미매칭 ({item_cd}), 건너뜀')
                    continue

                qty = int(detail.get('rcvQt', 0) or 0)
                if qty <= 0:
                    continue

                lot_str = detail.get('lotNb', '') or ''
                lot_date = None
                if lot_str:
                    try:
                        # LOT 형식: YYMMDD-001 또는 다양한 형식
                        lot_clean = lot_str.split('-')[0] if '-' in lot_str else lot_str[:6]
                        if len(lot_clean) == 6:
                            lot_date = datetime(2000 + int(lot_clean[:2]), int(lot_clean[2:4]), int(lot_clean[4:6])).date()
                    except (ValueError, IndexError):
                        pass

                detail_remark = detail.get('remarkDc', '') or ''

                # MaterialStock 증가 (원자적)
                result_stock = 0
                if warehouse:
                    stock, _ = MaterialStock.objects.get_or_create(
                        warehouse=warehouse,
                        part=part,
                        lot_no=lot_date,
                        defaults={'quantity': 0}
                    )
                    MaterialStock.objects.filter(id=stock.id).update(
                        quantity=models.F('quantity') + qty
                    )
                    stock.refresh_from_db()
                    result_stock = stock.quantity
                else:
                    logger.warning(f'창고 매칭 실패 - 재고 미반영: part={item_cd}')
                    result_stock = qty

                # MaterialTransaction 생성
                _create_trx(
                    transaction_type='IN_ERP',
                    date=rcv_date,
                    part=part,
                    lot_no=lot_date,
                    quantity=qty,
                    warehouse_to=warehouse,
                    result_stock=result_stock,
                    vendor=vendor,
                    remark=f'ERP입고({vendor_name}) {detail_remark}'.strip(),
                    erp_incoming_no=trx_key,
                    erp_sync_status='SUCCESS',
                    erp_sync_message=f'ERP 동기화 ({rcv_nb})',
                )
                existing_rcv_nbs.add(trx_key)

            synced += 1

        except Exception as e:
            errors += 1
            error_list.append(f'{rcv_nb}: {str(e)}')
            logger.error(f'ERP 입고 동기화 오류 ({rcv_nb}): {e}')

    # ── ERP에서 삭제된 건 감지 및 SCM 정리 ──
    deleted = 0
    # 조회 기간 내 ERP 입고번호 목록 (SCM발 제외)
    erp_rcv_nbs_in_period = set()
    if headers:
        for h in headers:
            rcv_nb = h.get('rcvNb', '')
            remark = h.get('remarkDc', '') or ''
            if rcv_nb and 'SCM' not in remark:
                erp_rcv_nbs_in_period.add(rcv_nb)

    # 같은 기간에 IN_ERP로 동기화된 SCM 레코드 중 ERP에 없는 건 찾기
    from datetime import date as date_cls
    try:
        dt_from = datetime.strptime(date_from, '%Y%m%d').date()
        dt_to = datetime.strptime(date_to, '%Y%m%d').date()
    except (ValueError, TypeError):
        dt_from = dt_to = None

    if dt_from and dt_to:
        orphan_trxs = MaterialTransaction.objects.filter(
            transaction_type='IN_ERP',
            date__date__gte=dt_from,
            date__date__lte=dt_to,
            erp_incoming_no__isnull=False,
        )
        for trx in orphan_trxs:
            # erp_incoming_no 형식: 'RV2602000222-1' → 입고번호는 앞부분
            rcv_nb_part = trx.erp_incoming_no.rsplit('-', 1)[0] if '-' in trx.erp_incoming_no else trx.erp_incoming_no
            if rcv_nb_part not in erp_rcv_nbs_in_period:
                # ERP에서 삭제된 건 → 재고 차감 후 삭제
                try:
                    if trx.warehouse_to and trx.part:
                        stock = MaterialStock.objects.filter(
                            warehouse=trx.warehouse_to, part=trx.part, lot_no=trx.lot_no
                        ).first()
                        if stock:
                            stock.quantity = max(0, stock.quantity - trx.quantity)
                            stock.save()
                    logger.info(f'ERP 삭제 감지: {trx.transaction_no} (ERP:{trx.erp_incoming_no}) 삭제')
                    trx.delete()
                    deleted += 1
                except Exception as e:
                    logger.error(f'ERP 삭제 감지 처리 오류 ({trx.transaction_no}): {e}')

    if deleted > 0:
        logger.info(f'ERP 삭제 감지 완료: {deleted}건 제거')

    logger.info(f'ERP 입고 동기화 완료: 신규 {synced}, 건너뜀 {skipped}, 삭제감지 {deleted}, 오류 {errors}')
    return synced, skipped, errors, error_list


# =============================================================================
# ERP 현재고 조회
# =============================================================================

def fetch_erp_stock(year=None, month=None, total_fg='0', wh_cd=None):
    """
    ERP 현재고 조회 (품목별)
    - year: 기준년도 (None이면 올해)
    - month: 기준월 '01'~'12' (None이면 해당 연도 전체/현재)
    - total_fg: '0'=품목별, '1'=LOT별
    - wh_cd: 창고코드 (None이면 전체)
    Returns: (success, items_list, error)
    """
    if not getattr(settings, 'ERP_ENABLED', False):
        return False, None, 'ERP 비활성화'

    if year is None:
        from datetime import datetime
        year = str(datetime.now().year)

    body = {
        'coCd': settings.ERP_COMPANY_CODE,
        'year': year,
        'totalFg': total_fg,
    }
    if month:
        body['month'] = month
    if wh_cd:
        body['whCd'] = wh_cd

    success, data, error = call_erp_api('/apiproxy/api20A02S01501', body)

    if success:
        items = data.get('resultData', [])
        return True, items, None
    else:
        return False, None, error


def init_stock_from_erp(year=None, cutoff_date=None):
    """
    ERP 현재고를 SCM 기초재고로 셋팅 (전체 초기화)
    - ERP 실시간 현재고(invQt1)를 가져와 SCM 재고를 초기화
    - cutoff_date: 동기화 기준일 (이 날짜 이후 ERP 수불만 동기화)
    - 기존 MaterialStock 전체 삭제 후 ERP 데이터로 생성
    - LOT=null (기초재고)
    Returns: dict {created, skipped_zero, skipped_no_part, skipped_no_wh, error}
    """
    from material.models import MaterialStock, Warehouse
    from orders.models import Part
    from datetime import datetime

    result = {
        'created': 0, 'skipped_zero': 0,
        'skipped_no_part': 0, 'skipped_no_wh': 0,
        'error': None,
    }

    # ERP 실시간 현재고 조회 (month 미지정 = 현시점 재고)
    query_year = year or str(datetime.now().year)
    query_month = None
    result['query_period'] = f'{query_year}년 실시간 현재고'
    logger.info(f'기초재고 조회: ERP 실시간 현재고 사용 (기준일: {cutoff_date or "오늘"})')

    # 진행률 캐시 초기화
    from django.core.cache import cache
    cache.set('erp_stock_init_progress', {'stage': 'ERP 재고 조회 중...', 'percent': 5}, timeout=300)

    ok, items, err = fetch_erp_stock(year=query_year, month=query_month, total_fg='0')
    if not ok:
        result['error'] = err or 'ERP 조회 실패'
        cache.delete('erp_stock_init_progress')
        return result
    if not items:
        result['error'] = 'ERP 재고 데이터 없음'
        cache.delete('erp_stock_init_progress')
        return result

    total_items = len(items)
    cache.set('erp_stock_init_progress', {
        'stage': f'ERP 데이터 {total_items}건 수신 완료, 기존 재고 삭제 중...',
        'percent': 15,
    }, timeout=300)

    MaterialStock.objects.all().delete()
    logger.info('기초재고 셋팅: 기존 MaterialStock 전체 삭제')

    part_map = {p.part_no: p for p in Part.objects.all()}
    wh_map = {w.code: w for w in Warehouse.objects.filter(is_active=True)}

    cache.set('erp_stock_init_progress', {
        'stage': f'재고 생성 중... (0/{total_items})',
        'percent': 20,
    }, timeout=300)

    for idx, item in enumerate(items):
        qty = item.get('invQt1', 0) or 0
        if qty <= 0:
            result['skipped_zero'] += 1
            continue
        part = part_map.get(item.get('itemCd', ''))
        if not part:
            result['skipped_no_part'] += 1
            continue
        warehouse = wh_map.get(item.get('whCd', ''))
        if not warehouse:
            result['skipped_no_wh'] += 1
            continue
        MaterialStock.objects.create(
            warehouse=warehouse, part=part, lot_no=None, quantity=int(qty),
        )
        result['created'] += 1

        # 50건마다 진행률 업데이트
        if (idx + 1) % 50 == 0 or idx == total_items - 1:
            pct = 20 + int((idx + 1) / total_items * 70)  # 20% ~ 90%
            cache.set('erp_stock_init_progress', {
                'stage': f'재고 생성 중... ({idx + 1}/{total_items})',
                'percent': pct,
                'created': result['created'],
            }, timeout=300)

    cache.set('erp_stock_init_progress', {
        'stage': '기준일 저장 중...',
        'percent': 95,
    }, timeout=300)

    # 동기화 시작일 저장 → sync 함수들이 이 날짜 이후 건만 처리
    # 실시간 현재고에는 오늘까지 수불이 모두 반영되어 있으므로, 내일부터 동기화
    import re
    from datetime import timedelta

    if cutoff_date:
        base = datetime.strptime(cutoff_date, '%Y-%m-%d')
    else:
        base = datetime.now()
    sync_start = (base + timedelta(days=1)).strftime('%Y%m%d')
    cache.set('erp_stock_init_date', sync_start, timeout=None)

    # settings.py에도 영구 저장 (서버 재시작 대비)
    try:
        settings_path = settings.BASE_DIR / 'config' / 'settings.py'
        with open(settings_path, 'r', encoding='utf-8') as f:
            content = f.read()
        content = re.sub(
            r"ERP_STOCK_INIT_DATE\s*=\s*'[^']*'",
            f"ERP_STOCK_INIT_DATE = '{sync_start}'",
            content
        )
        with open(settings_path, 'w', encoding='utf-8') as f:
            f.write(content)
    except Exception as e:
        logger.warning(f'settings.py 기준일 저장 실패 (cache는 저장됨): {e}')

    result['cutoff_date'] = sync_start

    cache.set('erp_stock_init_progress', {
        'stage': '완료!',
        'percent': 100,
        'created': result['created'],
    }, timeout=30)

    logger.info(f'기초재고 셋팅 완료: 생성 {result["created"]}건, 동기화시작일={sync_start}')
    return result


def compare_erp_stock(year=None):
    """
    ERP 현재고 vs SCM 재고 비교
    Returns: (success, comparison_list, summary, error)
    """
    from material.models import MaterialStock, Warehouse
    from orders.models import Part
    from django.db.models import Sum
    from django.core.cache import cache

    cache.set('erp_sync_progress', {'stage': 'ERP 재고 조회 중...', 'percent': 10}, timeout=300)

    ok, items, err = fetch_erp_stock(year=year, total_fg='0')
    if not ok:
        cache.delete('erp_sync_progress')
        return False, None, None, err or 'ERP 조회 실패'

    cache.set('erp_sync_progress', {'stage': '데이터 비교 중...', 'percent': 50}, timeout=300)

    erp_map = {}
    erp_info = {}
    for item in (items or []):
        qty = item.get('invQt1', 0) or 0
        if qty <= 0:
            continue
        key = (item.get('whCd', ''), item.get('itemCd', ''))
        erp_map[key] = int(qty)
        erp_info[item.get('itemCd', '')] = item.get('itemNm', '')

    scm_agg = MaterialStock.objects.values(
        'warehouse__code', 'part__part_no'
    ).annotate(total=Sum('quantity'))
    scm_map = {}
    for row in scm_agg:
        key = (row['warehouse__code'], row['part__part_no'])
        scm_map[key] = int(row['total'] or 0)

    all_keys = set(erp_map.keys()) | set(scm_map.keys())
    wh_names = {w.code: w.name for w in Warehouse.objects.all()}
    part_names = {p.part_no: p.part_name for p in Part.objects.all()}

    comparison = []
    summary = {'total': 0, 'match': 0, 'over': 0, 'under': 0, 'erp_only': 0, 'scm_only': 0}

    for wh_cd, item_cd in sorted(all_keys):
        erp_qty = erp_map.get((wh_cd, item_cd), 0)
        scm_qty = scm_map.get((wh_cd, item_cd), 0)
        diff = scm_qty - erp_qty
        part_name = part_names.get(item_cd, '') or erp_info.get(item_cd, '')

        comparison.append({
            'part_no': item_cd, 'part_name': part_name,
            'wh_code': wh_cd, 'wh_name': wh_names.get(wh_cd, wh_cd),
            'erp_qty': erp_qty, 'scm_qty': scm_qty, 'diff': diff,
        })

        summary['total'] += 1
        if diff == 0:
            summary['match'] += 1
        elif erp_qty == 0:
            summary['scm_only'] += 1
        elif scm_qty == 0:
            summary['erp_only'] += 1
        elif diff > 0:
            summary['over'] += 1
        else:
            summary['under'] += 1

    cache.set('erp_sync_progress', {'stage': '완료!', 'percent': 100, 'detail': f'전체 {summary["total"]}, 일치 {summary["match"]}'}, timeout=30)
    return True, comparison, summary, None


def adjust_stock_to_erp():
    """
    SCM 재고를 ERP 실시간 현재고에 맞춰 조정
    - 차이 나는 품목만 증감 처리 (전체 삭제 X)
    - ADJ_ERP_IN / ADJ_ERP_OUT 트랜잭션 생성 (이력 추적)
    - 동기화 시작일을 오늘로 설정
    Returns: dict {adjusted, increased, decreased, skipped_no_part, skipped_no_wh, error}
    """
    from material.models import MaterialStock, MaterialTransaction, Warehouse
    from orders.models import Part
    from django.db.models import Sum
    from django.utils import timezone
    from datetime import datetime, timedelta
    from django.core.cache import cache as _cache

    result = {
        'adjusted': 0, 'increased': 0, 'decreased': 0,
        'skipped_no_part': 0, 'skipped_no_wh': 0, 'error': None,
    }

    _cache.set('erp_sync_progress', {'stage': 'ERP 현재고 조회 중...', 'percent': 5}, timeout=300)

    ok, items, err = fetch_erp_stock(year=str(datetime.now().year), month=None, total_fg='0')
    if not ok:
        result['error'] = err or 'ERP 조회 실패'
        _cache.delete('erp_sync_progress')
        return result
    if not items:
        result['error'] = 'ERP 재고 데이터 없음'
        _cache.delete('erp_sync_progress')
        return result

    _cache.set('erp_sync_progress', {'stage': 'SCM 재고 비교 중...', 'percent': 15}, timeout=300)

    # ERP 재고 맵: (whCd, itemCd) → qty
    erp_map = {}
    for item in items:
        qty = int(item.get('invQt1', 0) or 0)
        key = (item.get('whCd', ''), item.get('itemCd', ''))
        erp_map[key] = erp_map.get(key, 0) + qty  # 혹시 중복행이면 합산

    # SCM 재고 맵: (warehouse_code, part_no) → total qty
    scm_agg = MaterialStock.objects.values(
        'warehouse__code', 'part__part_no'
    ).annotate(total=Sum('quantity'))
    scm_map = {}
    for row in scm_agg:
        key = (row['warehouse__code'], row['part__part_no'])
        scm_map[key] = int(row['total'] or 0)

    part_map = {p.part_no: p for p in Part.objects.all()}
    wh_map = {w.code: w for w in Warehouse.objects.filter(is_active=True)}

    all_keys = list(set(erp_map.keys()) | set(scm_map.keys()))
    total_keys = len(all_keys)
    now = timezone.now()

    for idx, (wh_cd, item_cd) in enumerate(all_keys):
        erp_qty = erp_map.get((wh_cd, item_cd), 0)
        scm_qty = scm_map.get((wh_cd, item_cd), 0)
        diff = erp_qty - scm_qty  # 양수면 SCM 부족, 음수면 SCM 초과

        if diff == 0:
            continue

        part = part_map.get(item_cd)
        if not part:
            result['skipped_no_part'] += 1
            continue
        warehouse = wh_map.get(wh_cd)
        if not warehouse:
            result['skipped_no_wh'] += 1
            continue

        # MaterialStock 조정 (lot_no=None 기초재고 레코드)
        stock = MaterialStock.objects.filter(
            warehouse=warehouse, part=part, lot_no=None
        ).first()

        if diff > 0:
            # SCM 부족 → 증가
            if stock:
                stock.quantity += diff
                stock.save()
            else:
                MaterialStock.objects.create(
                    warehouse=warehouse, part=part, lot_no=None, quantity=diff
                )
            trx_type = 'ADJ_ERP_IN'
            result['increased'] += 1
        else:
            # SCM 초과 → 감소
            abs_diff = abs(diff)
            if stock:
                stock.quantity = max(0, stock.quantity - abs_diff)
                stock.save()
            trx_type = 'ADJ_ERP_OUT'
            result['decreased'] += 1

        # 이력 생성
        _create_trx(
            transaction_type=trx_type,
            part=part,
            warehouse_to=warehouse if trx_type == 'ADJ_ERP_IN' else None,
            warehouse_from=warehouse if trx_type == 'ADJ_ERP_OUT' else None,
            quantity=abs(diff) if trx_type == 'ADJ_ERP_IN' else -abs(diff),
            lot_no=None,
            date=now,
            remark=f'ERP 재고조정 (ERP={erp_qty}, SCM={scm_qty}, diff={diff:+d})',
        )
        result['adjusted'] += 1

        if (idx + 1) % 100 == 0 or idx == total_keys - 1:
            pct = 20 + int((idx + 1) / total_keys * 70)
            _cache.set('erp_sync_progress', {
                'stage': f'재고 조정 중... ({idx + 1}/{total_keys})',
                'percent': pct,
                'detail': f'조정 {result["adjusted"]}건 (증가 {result["increased"]}, 감소 {result["decreased"]})',
            }, timeout=300)

    # 동기화 시작일: 오늘 (오늘 오후 수불도 자동동기화 대상에 포함)
    import re
    sync_start = datetime.now().strftime('%Y%m%d')
    _cache.set('erp_stock_init_date', sync_start, timeout=None)

    try:
        settings_path = settings.BASE_DIR / 'config' / 'settings.py'
        with open(settings_path, 'r', encoding='utf-8') as f:
            content = f.read()
        content = re.sub(
            r"ERP_STOCK_INIT_DATE\s*=\s*'[^']*'",
            f"ERP_STOCK_INIT_DATE = '{sync_start}'",
            content
        )
        with open(settings_path, 'w', encoding='utf-8') as f:
            f.write(content)
    except Exception as e:
        logger.warning(f'settings.py 동기화 시작일 저장 실패: {e}')

    result['sync_start'] = sync_start
    _cache.set('erp_sync_progress', {'stage': '완료!', 'percent': 100, 'detail': f'조정 {result["adjusted"]}건'}, timeout=30)
    logger.info(f'ERP 재고조정 완료: 조정 {result["adjusted"]}건 (증가 {result["increased"]}, 감소 {result["decreased"]}), 동기화시작일={sync_start}')
    return result


# =============================================================================
# ERP 거래처 동기화
# =============================================================================

def fetch_erp_vendors(tr_fg='1'):
    """
    ERP 거래처 조회 (일반거래처, 사용중)
    - tr_fg: 거래처 구분 (1.일반, 2.수출 등)
    Returns: (success, items_list, error)
    """
    all_items = []
    offset = 0
    page_size = 500

    while True:
        body = {
            'coCd': settings.ERP_COMPANY_CODE,
            'trFg': tr_fg,
            'usePagination': True,
            'pagingOffset': offset,
            'pagingCount': page_size,
        }

        success, data, error = call_erp_api('/apiproxy/api16S11', body)
        if not success:
            return False, all_items, error

        items = data.get('resultData') or []
        if not items:
            break

        all_items.extend(items)
        logger.info(f'ERP 거래처 조회: offset={offset}, 건수={len(items)}')

        if len(items) < page_size:
            break
        offset += page_size

    return True, all_items, None


def sync_erp_vendors():
    """
    ERP 거래처 → SCM Vendor 동기화
    - ERP trCd 기준으로 매칭 (erp_code 또는 code)
    - 없으면 신규 생성, 있으면 업데이트
    Returns: dict with created, updated, skipped, errors, total
    """
    from orders.models import Vendor
    from django.core.cache import cache

    result = {'created': 0, 'updated': 0, 'skipped': 0, 'errors': [], 'total': 0}

    cache.set('erp_sync_progress', {'stage': 'ERP 거래처 조회 중...', 'percent': 5}, timeout=300)

    success, items, error = fetch_erp_vendors()
    if not success:
        result['errors'].append(f'ERP 거래처 조회 실패: {error}')
        cache.delete('erp_sync_progress')
        return result

    result['total'] = len(items)
    cache.set('erp_sync_progress', {'stage': f'거래처 {len(items)}건 처리 중...', 'percent': 10}, timeout=300)

    for idx, item in enumerate(items):
        tr_cd = (item.get('trCd') or '').strip()
        tr_nm = (item.get('trNm') or '').strip()

        if not tr_cd or not tr_nm:
            result['skipped'] += 1
            continue

        # useYn이 '1'인 것만 (사용중)
        if item.get('useYn') != '1':
            result['skipped'] += 1
            continue

        try:
            # erp_code로 먼저 찾고, 없으면 code로 찾기
            vendor = Vendor.objects.filter(erp_code=tr_cd).first()
            if not vendor:
                vendor = Vendor.objects.filter(code=tr_cd).first()

            if vendor:
                # 업데이트
                vendor.name = tr_nm
                vendor.erp_code = tr_cd
                vendor.biz_registration_number = (item.get('regNb') or '').strip() or vendor.biz_registration_number
                vendor.representative = (item.get('ceoNm') or '').strip() or vendor.representative
                vendor.biz_type = (item.get('business') or '').strip() or vendor.biz_type
                vendor.biz_item = (item.get('jongmok') or '').strip() or vendor.biz_item
                addr1 = (item.get('divAddr1') or '').strip()
                addr2 = (item.get('addr2') or '').strip()
                if addr1:
                    vendor.address = f'{addr1} {addr2}'.strip()
                vendor.save()
                result['updated'] += 1
            else:
                # 신규 생성
                Vendor.objects.create(
                    code=tr_cd,
                    erp_code=tr_cd,
                    name=tr_nm,
                    biz_registration_number=(item.get('regNb') or '').strip(),
                    representative=(item.get('ceoNm') or '').strip(),
                    biz_type=(item.get('business') or '').strip(),
                    biz_item=(item.get('jongmok') or '').strip(),
                    address=f"{(item.get('divAddr1') or '').strip()} {(item.get('addr2') or '').strip()}".strip(),
                )
                result['created'] += 1

        except Exception as e:
            result['errors'].append(f'{tr_cd} {tr_nm}: {str(e)}')
            logger.error(f'거래처 동기화 오류: {tr_cd} -> {e}')

        if (idx + 1) % 50 == 0 or idx == len(items) - 1:
            pct = 10 + int((idx + 1) / len(items) * 85)
            cache.set('erp_sync_progress', {
                'stage': f'거래처 처리 중... ({idx + 1}/{len(items)})',
                'percent': pct,
                'detail': f'신규 {result["created"]}, 갱신 {result["updated"]}',
            }, timeout=300)

    cache.set('erp_sync_progress', {'stage': '완료!', 'percent': 100, 'detail': f'신규 {result["created"]}, 갱신 {result["updated"]}'}, timeout=30)
    logger.info(f'ERP 거래처 동기화 완료: {result}')
    return result


# =============================================================================
# ERP 품목 동기화
# =============================================================================

def fetch_erp_items(use_yn='1'):
    """
    ERP 품목정보 조회 (사용중인 품목 전체)
    Returns: (success, items_list, error)
    """
    body = {
        'coCd': settings.ERP_COMPANY_CODE,
        'useYn': use_yn,
    }

    success, data, error = call_erp_api('/apiproxy/api20A00S00701', body)
    if not success:
        return False, [], error

    items = data.get('resultData') or []
    logger.info(f'ERP 품목 조회: {len(items)}건')
    return True, items, None


def sync_erp_items():
    """
    ERP 품목 → SCM Part 동기화
    - itemCd(품번) 기준으로 매칭
    - 없으면 신규 생성, 있으면 업데이트
    - acctFg → account_type 매핑
    - trmainCd → vendor FK 매핑 (erp_code 기준)
    Returns: dict with created, updated, skipped, errors, total
    """
    from orders.models import Part, Vendor
    from django.core.cache import cache

    ACCT_MAP = {
        '0': 'RAW',       # 원재료
        '1': 'RAW',       # 부재료 → 원재료
        '2': 'FINISHED',  # 제품
        '4': 'RAW',       # 반제품 → 원재료
        '5': 'PRODUCT',   # 상품
        '6': 'RAW',       # 저장품 → 원재료
    }

    result = {'created': 0, 'updated': 0, 'skipped': 0, 'errors': [], 'total': 0}

    cache.set('erp_sync_progress', {'stage': 'ERP 품목 조회 중...', 'percent': 5}, timeout=300)

    success, items, error = fetch_erp_items()
    if not success:
        result['errors'].append(f'ERP 품목 조회 실패: {error}')
        cache.delete('erp_sync_progress')
        return result

    result['total'] = len(items)
    cache.set('erp_sync_progress', {'stage': f'품목 {len(items)}건 처리 중...', 'percent': 10}, timeout=300)

    # 거래처 캐시 (erp_code → Vendor)
    vendor_cache = {}
    for v in Vendor.objects.filter(erp_code__isnull=False).exclude(erp_code=''):
        vendor_cache[v.erp_code] = v

    for idx, item in enumerate(items):
        item_cd = (item.get('itemCd') or '').strip()
        item_nm = (item.get('itemNm') or '').strip()

        if not item_cd:
            result['skipped'] += 1
            continue

        acct_fg = (item.get('acctFg') or '0').strip()
        account_type = ACCT_MAP.get(acct_fg, 'RAW')
        part_group = (item.get('itemgrpNm') or '일반').strip() or '일반'

        # 주거래처 매핑
        trmain_cd = (item.get('trmainCd') or '').strip()
        vendor = vendor_cache.get(trmain_cd)

        try:
            part = Part.objects.filter(part_no=item_cd).first()

            if part:
                # 업데이트
                part.part_name = item_nm or part.part_name
                part.account_type = account_type
                part.part_group = part_group
                if vendor:
                    part.vendor = vendor
                part.save()
                result['updated'] += 1
            else:
                # 신규 생성
                Part.objects.create(
                    part_no=item_cd,
                    part_name=item_nm or item_cd,
                    account_type=account_type,
                    part_group=part_group,
                    vendor=vendor,
                )
                result['created'] += 1

        except Exception as e:
            result['errors'].append(f'{item_cd} {item_nm}: {str(e)}')
            logger.error(f'품목 동기화 오류: {item_cd} -> {e}')

        if (idx + 1) % 100 == 0 or idx == len(items) - 1:
            pct = 10 + int((idx + 1) / len(items) * 85)
            cache.set('erp_sync_progress', {
                'stage': f'품목 처리 중... ({idx + 1}/{len(items)})',
                'percent': pct,
                'detail': f'신규 {result["created"]}, 갱신 {result["updated"]}',
            }, timeout=300)

    cache.set('erp_sync_progress', {'stage': '완료!', 'percent': 100, 'detail': f'신규 {result["created"]}, 갱신 {result["updated"]}'}, timeout=30)
    logger.info(f'ERP 품목 동기화 완료: {result}')
    return result


# =============================================================================
# ERP 입고이력 기반 업체 자동 연결
# =============================================================================

def link_vendor_by_incoming(months=6):
    """
    ERP 입고이력에서 품번↔거래처 매핑을 추출하여
    SCM Part.vendor가 null인 품목에 자동 연결
    Returns: dict {total_headers, matched, updated, skipped_no_vendor, errors}
    """
    from orders.models import Part, Vendor
    from django.core.cache import cache
    from datetime import datetime, timedelta

    result = {
        'total_headers': 0, 'matched': 0, 'updated': 0,
        'skipped_no_vendor': 0, 'errors': [],
        'updated_list': [],       # 연결 성공 목록
        'skipped_list': [],       # 거래처없음 목록
    }

    # 날짜 범위 계산
    date_to = datetime.now().strftime('%Y%m%d')
    date_from = (datetime.now() - timedelta(days=months * 30)).strftime('%Y%m%d')

    cache.set('erp_sync_progress', {'stage': f'ERP 입고 헤더 조회 중... ({months}개월)', 'percent': 5}, timeout=600)

    ok, headers, err = fetch_erp_incoming_headers(date_from, date_to)
    if not ok:
        result['errors'].append(f'ERP 입고 조회 실패: {err}')
        cache.delete('erp_sync_progress')
        return result

    result['total_headers'] = len(headers)
    cache.set('erp_sync_progress', {
        'stage': f'입고 {len(headers)}건 디테일 조회 중...',
        'percent': 10,
    }, timeout=600)

    # 품번 → 거래처코드 매핑 수집
    item_vendor_map = {}  # itemCd -> (trCd, vendorName)
    for idx, h in enumerate(headers):
        rcv_nb = h.get('rcvNb', '')
        tr_cd = (h.get('trCd', '') or '').strip()
        vendor_nm = h.get('attrNm', '')
        if not rcv_nb or not tr_cd:
            continue

        ok2, details, _ = fetch_erp_incoming_detail(rcv_nb)
        if ok2 and details:
            for d in details:
                item_cd = (d.get('itemCd', '') or '').strip()
                if item_cd:
                    item_vendor_map[item_cd] = (tr_cd, vendor_nm)

        if (idx + 1) % 50 == 0 or idx == len(headers) - 1:
            pct = 10 + int((idx + 1) / len(headers) * 60)
            cache.set('erp_sync_progress', {
                'stage': f'입고 디테일 조회 중... ({idx + 1}/{len(headers)})',
                'percent': pct,
                'detail': f'품번-거래처 매핑: {len(item_vendor_map)}건',
            }, timeout=600)

    result['matched'] = len(item_vendor_map)

    cache.set('erp_sync_progress', {
        'stage': '업체 미연결 품목 업데이트 중...',
        'percent': 75,
        'detail': f'매핑 {len(item_vendor_map)}건',
    }, timeout=600)

    # Vendor 캐시
    vendor_cache = {}
    for v in Vendor.objects.filter(erp_code__isnull=False).exclude(erp_code=''):
        vendor_cache[v.erp_code] = v

    # 미연결 Part 업데이트
    no_vendor_parts = Part.objects.filter(vendor__isnull=True)
    total_parts = no_vendor_parts.count()
    updated = 0

    for idx, part in enumerate(no_vendor_parts):
        mapping = item_vendor_map.get(part.part_no)
        if mapping:
            tr_cd, vendor_nm = mapping
            vendor = vendor_cache.get(tr_cd)
            if vendor:
                part.vendor = vendor
                part.save(update_fields=['vendor'])
                updated += 1
                result['updated_list'].append({
                    'part_no': part.part_no,
                    'part_name': part.part_name,
                    'vendor_name': vendor.name,
                    'vendor_code': tr_cd,
                })
            else:
                result['skipped_no_vendor'] += 1
                result['skipped_list'].append({
                    'part_no': part.part_no,
                    'part_name': part.part_name,
                    'erp_vendor_code': tr_cd,
                    'erp_vendor_name': vendor_nm,
                })

        if (idx + 1) % 200 == 0 or idx == total_parts - 1:
            pct = 75 + int((idx + 1) / max(total_parts, 1) * 20)
            cache.set('erp_sync_progress', {
                'stage': f'업체 연결 중... ({idx + 1}/{total_parts})',
                'percent': pct,
                'detail': f'연결: {updated}건',
            }, timeout=600)

    result['updated'] = updated
    cache.set('erp_sync_progress', {
        'stage': '완료!',
        'percent': 100,
        'detail': f'연결: {updated}건 (매핑 {len(item_vendor_map)}건 중)',
    }, timeout=30)

    logger.info(f'입고이력 기반 업체 연결 완료: 매핑 {len(item_vendor_map)}건, 연결 {updated}건')
    return result


# =============================================================================
# ERP 재고조정 동기화 (ERP → SCM)
# =============================================================================

def fetch_erp_adjustment_headers(from_dt, to_dt, adjust_fg=''):
    """
    ERP 재고조정 헤더 내역 조회
    - from_dt/to_dt: 'YYYYMMDD' 형식
    - adjust_fg: '' 전체, '0' 기초, '1' 입고, '2' 출고
    Returns: (success, items_list, error)
    """
    if not getattr(settings, 'ERP_ENABLED', False):
        return False, None, 'ERP 비활성화'

    body = {
        'coCd': settings.ERP_COMPANY_CODE,
        'fromDt': from_dt,
        'toDt': to_dt,
    }
    if adjust_fg:
        body['adjustFg'] = adjust_fg

    success, data, error = call_erp_api('/apiproxy/api20A02S01301', body)

    if success:
        items = data.get('resultData', [])
        return True, items, None
    else:
        return False, None, error


def fetch_erp_adjustment_detail(adjust_nb, adjust_fg='1'):
    """
    ERP 재고조정 디테일 내역 조회
    - adjust_nb: 조정번호 (예: 'IA2306000001')
    - adjust_fg: 조정구분 (0.기초, 1.입고, 2.출고)
    Returns: (success, items_list, error)
    """
    if not getattr(settings, 'ERP_ENABLED', False):
        return False, None, 'ERP 비활성화'

    body = {
        'coCd': settings.ERP_COMPANY_CODE,
        'adjustNb': adjust_nb,
        'adjustFg': adjust_fg,
    }

    success, data, error = call_erp_api('/apiproxy/api20A02S01302', body)

    if success:
        items = data.get('resultData', [])
        return True, items, None
    else:
        return False, None, error


def sync_erp_adjustments(date_from=None, date_to=None):
    """
    ERP 재고조정 내역을 SCM에 동기화
    - 입고조정(adjustFg=1): SCM 재고 증가 (ADJ_ERP_IN)
    - 출고조정(adjustFg=2): SCM 재고 감소 (ADJ_ERP_OUT)
    - 기초조정(adjustFg=0): 건너뜀 (기초재고 셋팅은 별도)
    - SCM에서 등록한 건(remarkDc에 'SCM' 포함)은 제외
    - 이미 동기화된 건(erp_incoming_no에 조정번호 매칭)은 건너뜀
    Returns: (synced_count, skipped_count, error_count, error_list)
    """
    from material.models import MaterialTransaction, MaterialStock, Warehouse
    from orders.models import Part
    from django.db import models
    from django.utils import timezone as tz
    from django.core.cache import cache
    from datetime import datetime, timedelta

    if date_from is None:
        cutoff = cache.get('erp_stock_init_date') or getattr(settings, 'ERP_STOCK_INIT_DATE', '')
        if cutoff:
            date_from = cutoff
        else:
            date_from = (datetime.now() - timedelta(days=30)).strftime('%Y%m%d')
    if date_to is None:
        date_to = datetime.now().strftime('%Y%m%d')

    logger.info(f'ERP 재고조정 동기화 시작: {date_from} ~ {date_to}')

    # 입고조정 + 출고조정 조회 (기초조정은 제외)
    all_headers = []
    for fg in ['1', '2']:
        ok, headers, err = fetch_erp_adjustment_headers(date_from, date_to, adjust_fg=fg)
        if ok and headers:
            all_headers.extend(headers)
        elif not ok:
            logger.warning(f'ERP 재고조정 헤더 조회 실패 (adjustFg={fg}): {err}')

    if not all_headers:
        logger.info('ERP 재고조정 내역 없음')
        return 0, 0, 0, []

    # 이미 동기화된 조정번호 목록 (erp_incoming_no에 'IA' prefix로 저장)
    existing_nbs = set(
        MaterialTransaction.objects.filter(
            transaction_type__in=['ADJ_ERP_IN', 'ADJ_ERP_OUT'],
            erp_incoming_no__isnull=False,
        ).values_list('erp_incoming_no', flat=True)
    )

    synced = 0
    skipped = 0
    errors = 0
    error_list = []

    default_wh = Warehouse.objects.filter(code='2000').first()
    part_map = {p.part_no: p for p in Part.objects.all()}

    for header in all_headers:
        adjust_nb = header.get('adjustNb', '')
        if not adjust_nb:
            continue

        # SCM에서 등록한 건 제외
        remark = header.get('remarkDc', '') or ''
        if 'SCM' in remark:
            skipped += 1
            continue

        # 이미 동기화된 건 제외 (헤더 레벨 체크)
        if any(nb.startswith(adjust_nb) for nb in existing_nbs):
            skipped += 1
            continue

        adjust_fg = header.get('adjustFg', '')
        adjust_dt = header.get('adjustDt', '')
        wh_cd = header.get('whCd', '')

        # 입고조정/출고조정만 처리
        if adjust_fg not in ('1', '2'):
            skipped += 1
            continue

        try:
            # 디테일 조회
            ok2, details, err2 = fetch_erp_adjustment_detail(adjust_nb, adjust_fg)
            if not ok2 or not details:
                skipped += 1
                continue

            # Warehouse 매칭
            warehouse = None
            if wh_cd:
                warehouse = Warehouse.objects.filter(code=wh_cd).first()
            if not warehouse:
                warehouse = default_wh

            # 조정일 파싱 (ERP는 날짜만 제공 → 동기화 시점 시간 조합)
            try:
                erp_date = datetime.strptime(adjust_dt, '%Y%m%d').date()
                now = tz.now()
                adj_date = tz.make_aware(datetime.combine(erp_date, now.time()))
            except (ValueError, TypeError):
                adj_date = tz.now()

            for detail in details:
                item_cd = detail.get('itemCd', '')
                if not item_cd:
                    continue

                adjust_sq = detail.get('adjustSq', 1)
                trx_key = f'{adjust_nb}-{adjust_sq}'

                # 중복 체크
                if trx_key in existing_nbs:
                    continue

                part = part_map.get(item_cd)
                if not part:
                    logger.warning(f'ERP 재고조정: 품번 미매칭 ({item_cd}), 건너뜀')
                    continue

                # 수량 결정
                if adjust_fg == '1':  # 입고조정
                    qty = int(detail.get('rcvQt', 0) or 0)
                    trx_type = 'ADJ_ERP_IN'
                else:  # 출고조정
                    qty = int(detail.get('isuQt', 0) or 0)
                    trx_type = 'ADJ_ERP_OUT'

                if qty <= 0:
                    continue

                detail_remark = detail.get('remarkDc', '') or ''
                adjust_fg_nm = header.get('adjustFgNm', '')

                # MaterialStock 갱신 (원자적)
                result_stock = 0
                if warehouse:
                    stock, _ = MaterialStock.objects.get_or_create(
                        warehouse=warehouse,
                        part=part,
                        lot_no=None,
                        defaults={'quantity': 0}
                    )
                    if adjust_fg == '1':  # 입고 → 재고 증가
                        MaterialStock.objects.filter(id=stock.id).update(
                            quantity=models.F('quantity') + qty
                        )
                    else:  # 출고 → 재고 감소
                        from django.db.models.functions import Greatest
                        MaterialStock.objects.filter(id=stock.id).update(
                            quantity=Greatest(models.F('quantity') - qty, models.Value(0))
                        )
                    stock.refresh_from_db()
                    result_stock = stock.quantity
                else:
                    logger.warning(f'창고 매칭 실패 - 재고 미반영: part={item_cd}')
                    result_stock = qty

                # 입고조정 → warehouse_to, 출고조정 → warehouse_from
                trx_kwargs = {
                    'transaction_type': trx_type,
                    'date': adj_date,
                    'part': part,
                    'lot_no': None,
                    'quantity': qty if adjust_fg == '1' else -qty,
                    'result_stock': result_stock,
                    'remark': f'ERP {adjust_fg_nm}({adjust_nb}) {detail_remark}'.strip(),
                    'erp_incoming_no': trx_key,
                    'erp_sync_status': 'SUCCESS',
                    'erp_sync_message': f'ERP 재고조정 ({adjust_nb})',
                }
                if adjust_fg == '1':
                    trx_kwargs['warehouse_to'] = warehouse
                else:
                    trx_kwargs['warehouse_from'] = warehouse

                _create_trx(**trx_kwargs)
                existing_nbs.add(trx_key)

            synced += 1

        except Exception as e:
            errors += 1
            error_list.append(f'{adjust_nb}: {str(e)}')
            logger.error(f'ERP 재고조정 동기화 오류 ({adjust_nb}): {e}')

    logger.info(f'ERP 재고조정 동기화 완료: 신규 {synced}, 건너뜀 {skipped}, 오류 {errors}')
    return synced, skipped, errors, error_list


# ── ERP 발주 조회 API ──────────────────────────────────────────────

def fetch_erp_po_headers(date_from, date_to, tr_cd=None):
    """
    발주정보 헤더 내역 조회 (api20A02S00101)
    date_from, date_to: 'YYYYMMDD' 형식
    tr_cd: 거래처코드 (선택)
    Returns: list of header dicts
    """
    body = {
        'coCd': settings.ERP_COMPANY_CODE,
        'poDtFrom': date_from,
        'poDtTo': date_to,
    }
    if tr_cd:
        body['trCd'] = tr_cd

    success, data, error = call_erp_api('/apiproxy/api20A02S00101', body)
    if success and data:
        return data.get('resultData', []) or []
    if error:
        logger.warning(f'ERP 발주 헤더 조회 실패: {error}')
    return []


def fetch_erp_po_details(po_nb):
    """
    발주정보 디테일 내역 조회 (api20A02S00102)
    po_nb: 발주번호
    Returns: list of detail dicts
    """
    body = {
        'coCd': settings.ERP_COMPANY_CODE,
        'poNb': po_nb,
    }

    success, data, error = call_erp_api('/apiproxy/api20A02S00102', body)
    if success and data:
        return data.get('resultData', []) or []
    if error:
        logger.warning(f'ERP 발주 디테일 조회 실패 ({po_nb}): {error}')
    return []


# ── ERP 생산출고 API ──────────────────────────────────────────────

def fetch_erp_issue_headers(date_from, date_to):
    """
    생산출고 헤더 내역 조회 (api20A02S00801)
    date_from, date_to: 'YYYYMMDD' 형식
    Returns: (success, list, error)
    """
    body = {
        'coCd': settings.ERP_COMPANY_CODE,
        'isuDtFrom': date_from,
        'isuDtTo': date_to,
    }
    success, data, error = call_erp_api('/apiproxy/api20A02S00801', body)
    if success and data:
        return True, data.get('resultData', []) or [], None
    return False, None, error


def fetch_erp_issue_details(isu_nb):
    """
    생산출고 디테일 내역 조회 (api20A02S00802)
    isu_nb: 출고번호
    Returns: (success, list, error)
    """
    body = {
        'coCd': settings.ERP_COMPANY_CODE,
        'isuNb': isu_nb,
    }
    success, data, error = call_erp_api('/apiproxy/api20A02S00802', body)
    if success and data:
        return True, data.get('resultData', []) or [], None
    return False, None, error


def sync_erp_issue(date_from=None, date_to=None):
    """
    ERP 생산출고 내역을 WMS에 동기화
    - 원자재가 생산라인으로 출고된 건 → 재고 차감
    - 이미 동기화된 건(erp_incoming_no 매칭)은 건너뜀
    Returns: (synced_count, skipped_count, error_count, error_list)
    """
    from material.models import MaterialTransaction, MaterialStock, Warehouse
    from orders.models import Part
    from django.db import models
    from django.db.models.functions import Greatest
    from django.utils import timezone as tz
    from django.core.cache import cache
    from datetime import datetime, timedelta

    if date_from is None:
        cutoff = cache.get('erp_stock_init_date') or getattr(settings, 'ERP_STOCK_INIT_DATE', '')
        if cutoff:
            date_from = cutoff
        else:
            date_from = (datetime.now() - timedelta(days=1)).strftime('%Y%m%d')
    if date_to is None:
        date_to = datetime.now().strftime('%Y%m%d')

    logger.info(f'ERP 생산출고 동기화 시작: {date_from} ~ {date_to}')

    # 1) 헤더 조회
    ok, headers, err = fetch_erp_issue_headers(date_from, date_to)
    if not ok:
        logger.error(f'ERP 생산출고 헤더 조회 실패: {err}')
        return 0, 0, 1, [f'헤더 조회 실패: {err}']
    if not headers:
        logger.info('ERP 생산출고 내역 없음')
        return 0, 0, 0, []

    # 2) 이미 동기화된 출고번호 목록
    existing_nbs = set(
        MaterialTransaction.objects.filter(
            transaction_type='ISU_ERP',
            erp_incoming_no__isnull=False
        ).values_list('erp_incoming_no', flat=True)
    )

    synced = 0
    skipped = 0
    errors = 0
    error_list = []

    for header in headers:
        isu_nb = header.get('isuNb', '')
        if not isu_nb:
            continue

        # SCM에서 올린 건 제외
        remark = header.get('remarkDc', '') or ''
        if 'SCM' in remark:
            skipped += 1
            continue

        # 헤더 단위 중복 체크 (existing_nbs에는 '번호-순번' 형태이므로 prefix로 체크)
        if any(nb.startswith(isu_nb) for nb in existing_nbs):
            skipped += 1
            continue

        try:
            # 3) 디테일 조회
            ok2, details, err2 = fetch_erp_issue_details(isu_nb)
            if not ok2 or not details:
                skipped += 1
                continue

            isu_dt = header.get('isuDt', '')  # 'YYYYMMDD'

            # 출고일 파싱
            try:
                erp_date = datetime.strptime(isu_dt, '%Y%m%d').date()
                now = tz.localtime(tz.now())
                isu_date = now.replace(year=erp_date.year, month=erp_date.month, day=erp_date.day)
            except (ValueError, TypeError):
                isu_date = tz.now()

            detail_synced = False

            # 4) 디테일별 트랜잭션 생성
            for detail in details:
                item_cd = detail.get('itemCd', '')
                if not item_cd:
                    continue

                isu_sq = detail.get('isuSq', 1)
                trx_key = f'{isu_nb}-{isu_sq}'
                if trx_key in existing_nbs:
                    continue

                # Part 매칭
                part = Part.objects.filter(part_no=item_cd).first()
                if not part:
                    continue

                qty = int(detail.get('isuQt', 0) or 0)
                if qty <= 0:
                    continue

                # 출고창고 매칭
                fwh_cd = detail.get('fwhCd', '')
                warehouse = Warehouse.objects.filter(code=fwh_cd).first() if fwh_cd else None
                if not warehouse:
                    warehouse = Warehouse.objects.filter(code='2000').first()

                # MaterialStock 차감 (원자적)
                result_stock = 0
                if warehouse:
                    stock, _ = MaterialStock.objects.get_or_create(
                        warehouse=warehouse,
                        part=part,
                        lot_no=None,
                        defaults={'quantity': 0}
                    )
                    MaterialStock.objects.filter(id=stock.id).update(
                        quantity=Greatest(models.F('quantity') - qty, models.Value(0))
                    )
                    stock.refresh_from_db()
                    result_stock = stock.quantity

                detail_remark = detail.get('remarkDc', '') or ''
                fwh_nm = detail.get('fwhNm', '') or ''

                _create_trx(
                    transaction_type='ISU_ERP',
                    date=isu_date,
                    part=part,
                    lot_no=None,
                    quantity=-qty,
                    warehouse_from=warehouse,
                    result_stock=result_stock,
                    remark=f'ERP생산출고({fwh_nm}) {detail_remark}'.strip(),
                    erp_incoming_no=trx_key,
                    erp_sync_status='SUCCESS',
                    erp_sync_message=f'ERP 생산출고 동기화 ({isu_nb})',
                )
                existing_nbs.add(trx_key)
                detail_synced = True

            if detail_synced:
                synced += 1

        except Exception as e:
            errors += 1
            error_list.append(f'{isu_nb}: {str(e)}')
            logger.error(f'ERP 생산출고 동기화 오류 ({isu_nb}): {e}')

    logger.info(f'ERP 생산출고 동기화 완료: 신규 {synced}, 건너뜀 {skipped}, 오류 {errors}')
    return synced, skipped, errors, error_list


# ─────────────────────────────────────────────
# 생산입고 동기화 (ERP → SCM)
# ─────────────────────────────────────────────

def fetch_erp_receipt_list(date_from, date_to):
    """
    생산입고 내역 조회 (api20A02S00701)
    - 헤더+상세가 합쳐진 단일 리스트 구조
    date_from, date_to: 'YYYYMMDD' 형식
    Returns: (success, list, error)
    """
    body = {
        'coCd': settings.ERP_COMPANY_CODE,
        'rcvDtFrom': date_from,
        'rcvDtTo': date_to,
    }
    success, data, error = call_erp_api('/apiproxy/api20A02S00701', body)
    if success and data:
        return True, data.get('resultData', []) or [], None
    return False, None, error


def sync_erp_receipt(date_from=None, date_to=None):
    """
    ERP 생산입고 내역을 WMS에 동기화
    - 생산 완료된 품목이 창고에 입고 → 재고 증가
    - 이미 동기화된 건(erp_incoming_no 매칭)은 건너뜀
    Returns: (synced_count, skipped_count, error_count, error_list)
    """
    from material.models import MaterialTransaction, MaterialStock, Warehouse
    from orders.models import Part
    from django.db import models
    from django.utils import timezone as tz
    from django.core.cache import cache
    from datetime import datetime, timedelta

    if date_from is None:
        cutoff = cache.get('erp_stock_init_date') or getattr(settings, 'ERP_STOCK_INIT_DATE', '')
        if cutoff:
            date_from = cutoff
        else:
            date_from = (datetime.now() - timedelta(days=1)).strftime('%Y%m%d')
    if date_to is None:
        date_to = datetime.now().strftime('%Y%m%d')

    logger.info(f'ERP 생산입고 동기화 시작: {date_from} ~ {date_to}')

    # 1) 생산입고 내역 조회
    ok, items, err = fetch_erp_receipt_list(date_from, date_to)
    if not ok:
        logger.error(f'ERP 생산입고 조회 실패: {err}')
        return 0, 0, 1, [f'조회 실패: {err}']
    if not items:
        logger.info('ERP 생산입고 내역 없음')
        return 0, 0, 0, []

    # 2) 이미 동기화된 입고번호 목록
    existing_nbs = set(
        MaterialTransaction.objects.filter(
            transaction_type='RCV_ERP',
            erp_incoming_no__isnull=False
        ).values_list('erp_incoming_no', flat=True)
    )

    synced = 0
    skipped = 0
    errors = 0
    error_list = []

    for item in items:
        rcv_nb = item.get('rcvNb', '')
        if not rcv_nb:
            continue

        # SCM에서 올린 건 제외
        remark = item.get('remarkDc', '') or ''
        if 'SCM' in remark:
            skipped += 1
            continue

        # 중복 체크
        if rcv_nb in existing_nbs:
            skipped += 1
            continue

        try:
            item_cd = item.get('itemCd', '')
            if not item_cd:
                continue

            # Part 매칭
            part = Part.objects.filter(part_no=item_cd).first()
            if not part:
                continue

            qty = int(item.get('rcvQt', 0) or 0)
            if qty <= 0:
                continue

            # 입고일 파싱
            rcv_dt = item.get('rcvDt', '')
            try:
                erp_date = datetime.strptime(rcv_dt, '%Y%m%d').date()
                now = tz.localtime(tz.now())
                rcv_date = now.replace(year=erp_date.year, month=erp_date.month, day=erp_date.day)
            except (ValueError, TypeError):
                rcv_date = tz.now()

            # 입고창고 매칭 (twhCd = 입고 대상 창고)
            twh_cd = item.get('twhCd', '')
            warehouse = Warehouse.objects.filter(code=twh_cd).first() if twh_cd else None
            if not warehouse:
                warehouse = Warehouse.objects.filter(code='2000').first()

            # MaterialStock 증가 (원자적)
            result_stock = 0
            if warehouse:
                stock, _ = MaterialStock.objects.get_or_create(
                    warehouse=warehouse,
                    part=part,
                    lot_no=None,
                    defaults={'quantity': 0}
                )
                MaterialStock.objects.filter(id=stock.id).update(
                    quantity=models.F('quantity') + qty
                )
                stock.refresh_from_db()
                result_stock = stock.quantity
            else:
                logger.warning(f'창고 매칭 실패 - 재고 미반영: part={item_cd}')

            twh_nm = item.get('twhNm', '') or ''

            _create_trx(
                transaction_type='RCV_ERP',
                date=rcv_date,
                part=part,
                lot_no=None,
                quantity=qty,
                warehouse_to=warehouse,
                result_stock=result_stock,
                remark=f'ERP생산입고({twh_nm}) {remark}'.strip(),
                erp_incoming_no=rcv_nb,
                erp_sync_status='SUCCESS',
                erp_sync_message=f'ERP 생산입고 동기화 ({rcv_nb})',
            )
            existing_nbs.add(rcv_nb)
            synced += 1

        except Exception as e:
            errors += 1
            error_list.append(f'{rcv_nb}: {str(e)}')
            logger.error(f'ERP 생산입고 동기화 오류 ({rcv_nb}): {e}')

    logger.info(f'ERP 생산입고 동기화 완료: 신규 {synced}, 건너뜀 {skipped}, 오류 {errors}')
    return synced, skipped, errors, error_list


# =============================================================================
# SCM → ERP: 재고이동 등록/삭제
# =============================================================================

def register_erp_stock_move(trx, qty, from_warehouse_code, to_warehouse_code):
    """
    ERP 재고이동 등록 (api20A02I01101)
    - trx: MaterialTransaction 객체 (TRANSFER)
    - qty: 이동수량
    - from_warehouse_code: 출고창고 코드 (예: '2000')
    - to_warehouse_code: 입고창고 코드 (예: '4300')
    Returns: (success: bool, erp_no: str or None, error: str or None)
    """
    if not getattr(settings, 'ERP_ENABLED', False):
        return False, None, 'ERP 비활성화'

    # 이동일자
    if hasattr(trx.date, 'strftime'):
        key_dt = trx.date.strftime('%Y%m%d')
    else:
        key_dt = str(trx.date).replace('-', '')[:8]

    # LOT 번호 포맷
    lot_str = ''
    if trx.lot_no:
        if hasattr(trx.lot_no, 'strftime'):
            lot_str = trx.lot_no.strftime('%y%m%d') + '-001'
        else:
            lot_str = str(trx.lot_no).replace('-', '')[:6] + '-001'

    body = {
        'coCd': settings.ERP_COMPANY_CODE,
        'moveDt': key_dt,
        'fwhCd': from_warehouse_code,
        'flcCd': from_warehouse_code,
        'twhCd': to_warehouse_code,
        'tlcCd': to_warehouse_code,
        'empCd': getattr(settings, 'ERP_DEFAULT_EMP_CODE', '20240601'),
        'deptCd': getattr(settings, 'ERP_DEFAULT_DEPT_CODE', '0630'),
        'divCd': settings.ERP_DIVISION_CODE,
        'remarkDc': f'SCM 재고이동 ({trx.transaction_no})',
        'detail': [{
            'moveSq': 1,
            'itemCd': trx.part.part_no,
            'moveQt': qty,
            'lotNb': lot_str,
            'remarkDc': f'SCM ({trx.transaction_no})',
        }]
    }

    success, data, error = call_erp_api('/apiproxy/api20A02I01101', body)

    if success:
        erp_no = data.get('resultData', '')
        trx.erp_incoming_no = erp_no
        trx.erp_sync_status = 'SUCCESS'
        trx.erp_sync_message = f'ERP 재고이동번호: {erp_no}'
        trx.save(update_fields=['erp_incoming_no', 'erp_sync_status', 'erp_sync_message'])
        logger.info(f'ERP 재고이동 등록 성공: {trx.transaction_no} -> {erp_no}')
        return True, erp_no, None
    else:
        trx.erp_sync_status = 'FAILED'
        trx.erp_sync_message = error or 'Unknown error'
        trx.save(update_fields=['erp_sync_status', 'erp_sync_message'])
        logger.warning(f'ERP 재고이동 등록 실패: {trx.transaction_no} -> {error}')
        return False, None, error


def delete_erp_stock_move(erp_move_no):
    """
    ERP 재고이동 삭제 (api20A02D01101)
    - erp_move_no: ERP 재고이동번호
    Returns: (success: bool, error: str or None)
    """
    if not getattr(settings, 'ERP_ENABLED', False):
        return False, 'ERP 비활성화'

    if not erp_move_no:
        return False, 'ERP 재고이동번호 없음'

    body = {
        'coCd': settings.ERP_COMPANY_CODE,
        'moveNb': erp_move_no,
    }

    success, data, error = call_erp_api('/apiproxy/api20A02D01101', body)

    if success:
        logger.info(f'ERP 재고이동 삭제 성공: {erp_move_no}')
        return True, None
    else:
        logger.warning(f'ERP 재고이동 삭제 실패: {erp_move_no} -> {error}')
        return False, error


# ─────────────────────────────────────────────
# 재고이동 동기화 (ERP → SCM)
# ─────────────────────────────────────────────

def fetch_erp_transfer_headers(date_from, date_to):
    """
    ERP 재고이동 헤더 조회 (api20A02S01101)
    date_from, date_to: 'YYYYMMDD' 형식
    Returns: (success, list, error)
    """
    body = {
        'coCd': settings.ERP_COMPANY_CODE,
        'moveDtFrom': date_from,
        'moveDtTo': date_to,
    }
    success, data, error = call_erp_api('/apiproxy/api20A02S01101', body)
    if success and data:
        return True, data.get('resultData', []) or [], None
    return False, None, error


def fetch_erp_transfer_details(move_nb):
    """
    ERP 재고이동 디테일 조회 (api20A02S01102)
    move_nb: 이동번호
    Returns: (success, list, error)
    """
    body = {
        'coCd': settings.ERP_COMPANY_CODE,
        'moveNb': move_nb,
    }
    success, data, error = call_erp_api('/apiproxy/api20A02S01102', body)
    if success and data:
        return True, data.get('resultData', []) or [], None
    return False, None, error


def sync_erp_stock_transfer(date_from=None, date_to=None):
    """
    ERP 재고이동 내역을 SCM에 동기화
    - 출고창고 재고 차감, 입고창고 재고 증가
    - SCM에서 올린 건('SCM' 포함)은 제외
    - 이미 동기화된 건(erp_incoming_no 매칭)은 건너뜀
    Returns: (synced_count, skipped_count, error_count, error_list)
    """
    from material.models import MaterialTransaction, MaterialStock, Warehouse
    from orders.models import Part
    from django.db import models
    from django.db.models.functions import Greatest
    from django.utils import timezone as tz
    from django.core.cache import cache
    from datetime import datetime, timedelta

    if date_from is None:
        cutoff = cache.get('erp_stock_init_date') or getattr(settings, 'ERP_STOCK_INIT_DATE', '')
        if cutoff:
            date_from = cutoff
        else:
            date_from = (datetime.now() - timedelta(days=1)).strftime('%Y%m%d')
    if date_to is None:
        date_to = datetime.now().strftime('%Y%m%d')

    logger.info(f'ERP 재고이동 동기화 시작: {date_from} ~ {date_to}')

    # 1) 헤더 조회
    ok, headers, err = fetch_erp_transfer_headers(date_from, date_to)
    if not ok:
        logger.error(f'ERP 재고이동 헤더 조회 실패: {err}')
        return 0, 0, 1, [f'헤더 조회 실패: {err}']
    if not headers:
        logger.info('ERP 재고이동 내역 없음')
        return 0, 0, 0, []

    # 2) 이미 동기화된 이동번호 목록
    existing_nbs = set(
        MaterialTransaction.objects.filter(
            transaction_type='TRF_ERP',
            erp_incoming_no__isnull=False
        ).values_list('erp_incoming_no', flat=True)
    )

    synced = 0
    skipped = 0
    errors = 0
    error_list = []

    for header in headers:
        move_nb = header.get('moveNb', '')
        if not move_nb:
            continue

        # SCM에서 올린 건 제외
        remark = header.get('remarkDc', '') or ''
        if 'SCM' in remark:
            skipped += 1
            continue

        # 헤더 단위 중복 체크 (existing_nbs에는 '번호-순번' 형태이므로 prefix로 체크)
        if any(nb.startswith(move_nb) for nb in existing_nbs):
            skipped += 1
            continue

        try:
            # 3) 디테일 조회
            ok2, details, err2 = fetch_erp_transfer_details(move_nb)
            if not ok2 or not details:
                skipped += 1
                continue

            move_dt = header.get('moveDt', '')  # 'YYYYMMDD'

            # 이동일 파싱
            try:
                erp_date = datetime.strptime(move_dt, '%Y%m%d').date()
                now = tz.localtime(tz.now())
                trf_date = now.replace(year=erp_date.year, month=erp_date.month, day=erp_date.day)
            except (ValueError, TypeError):
                trf_date = tz.now()

            detail_synced = False

            # 4) 디테일별 트랜잭션 생성
            for detail in details:
                item_cd = detail.get('itemCd', '')
                if not item_cd:
                    continue

                move_sq = detail.get('moveSq', 1)
                trx_key = f'{move_nb}-{move_sq}'
                if trx_key in existing_nbs:
                    continue

                # Part 매칭
                part = Part.objects.filter(part_no=item_cd).first()
                if not part:
                    continue

                qty = int(detail.get('moveQt', 0) or 0)
                if qty <= 0:
                    continue

                # 디테일에서 출고/입고 창고 가져오기
                fwh_cd = detail.get('fwhCd', '')
                twh_cd = detail.get('twhCd', '')

                # 출고창고 매칭
                from_wh = Warehouse.objects.filter(code=fwh_cd).first() if fwh_cd else None
                # 입고창고 매칭
                to_wh = Warehouse.objects.filter(code=twh_cd).first() if twh_cd else None

                if not from_wh:
                    from_wh = Warehouse.objects.filter(code='2000').first()
                if not to_wh:
                    to_wh = Warehouse.objects.filter(code='2000').first()

                # 출고창고 재고 차감
                from_result = 0
                if from_wh:
                    from_stock = MaterialStock.objects.filter(
                        warehouse=from_wh, part=part, lot_no=None
                    ).first()
                    if not from_stock:
                        from_stock = MaterialStock.objects.create(
                            warehouse=from_wh, part=part, lot_no=None, quantity=0
                        )
                    MaterialStock.objects.filter(id=from_stock.id).update(
                        quantity=Greatest(models.F('quantity') - qty, models.Value(0))
                    )
                    from_stock.refresh_from_db()
                    from_result = from_stock.quantity

                # 입고창고 재고 증가
                to_result = 0
                if to_wh:
                    to_stock = MaterialStock.objects.filter(
                        warehouse=to_wh, part=part, lot_no=None
                    ).first()
                    if not to_stock:
                        to_stock = MaterialStock.objects.create(
                            warehouse=to_wh, part=part, lot_no=None, quantity=0
                        )
                    MaterialStock.objects.filter(id=to_stock.id).update(
                        quantity=models.F('quantity') + qty
                    )
                    to_stock.refresh_from_db()
                    to_result = to_stock.quantity

                fwh_nm = detail.get('fwhNm', '') or fwh_cd
                twh_nm = detail.get('twhNm', '') or twh_cd
                detail_remark = detail.get('remarkDc', '') or ''

                _create_trx(
                    transaction_type='TRF_ERP',
                    date=trf_date,
                    part=part,
                    lot_no=None,
                    quantity=qty,
                    warehouse_from=from_wh,
                    warehouse_to=to_wh,
                    result_stock=to_result,
                    remark=f'ERP재고이동({fwh_nm}→{twh_nm}) {detail_remark}'.strip(),
                    erp_incoming_no=trx_key,
                    erp_sync_status='SUCCESS',
                    erp_sync_message=f'ERP 재고이동 동기화 ({move_nb})',
                )
                existing_nbs.add(trx_key)
                detail_synced = True

            if detail_synced:
                synced += 1

        except Exception as e:
            errors += 1
            error_list.append(f'{move_nb}: {str(e)}')
            logger.error(f'ERP 재고이동 동기화 오류 ({move_nb}): {e}')

    logger.info(f'ERP 재고이동 동기화 완료: 신규 {synced}, 건너뜀 {skipped}, 오류 {errors}')
    return synced, skipped, errors, error_list


# ─────────────────────────────────────────────
# 고객출고 동기화 (ERP → SCM)
# ─────────────────────────────────────────────

def fetch_erp_outgoing_headers(date_from, date_to):
    """
    ERP 출고 헤더 조회 (api20A01S00201)
    date_from, date_to: 'YYYYMMDD' 형식
    Returns: (success, list, error)
    """
    body = {
        'coCd': settings.ERP_COMPANY_CODE,
        'isuDtFrom': date_from,
        'isuDtTo': date_to,
    }
    success, data, error = call_erp_api('/apiproxy/api20A01S00201', body)
    if success and data:
        return True, data.get('resultData', []) or [], None
    return False, None, error


def fetch_erp_outgoing_details(isu_nb):
    """
    ERP 출고 디테일 조회 (api20A01S00202)
    isu_nb: 출고번호
    Returns: (success, list, error)
    """
    body = {
        'coCd': settings.ERP_COMPANY_CODE,
        'isuNb': isu_nb,
    }
    success, data, error = call_erp_api('/apiproxy/api20A01S00202', body)
    if success and data:
        return True, data.get('resultData', []) or [], None
    return False, None, error


def sync_erp_outgoing(date_from=None, date_to=None):
    """
    ERP 고객출고(물류출고) 내역을 WMS에 동기화
    - 고객에게 출고된 건 → 재고 차감
    - 이미 동기화된 건(erp_incoming_no 매칭)은 건너뜀
    - API: api20A01S00201(헤더) / api20A01S00202(디테일)
    Returns: (synced_count, skipped_count, error_count, error_list)
    """
    from material.models import MaterialTransaction, MaterialStock, Warehouse
    from orders.models import Part
    from django.db import models
    from django.db.models.functions import Greatest
    from django.utils import timezone as tz
    from django.core.cache import cache
    from datetime import datetime, timedelta

    if date_from is None:
        cutoff = cache.get('erp_stock_init_date') or getattr(settings, 'ERP_STOCK_INIT_DATE', '')
        if cutoff:
            date_from = cutoff
        else:
            date_from = (datetime.now() - timedelta(days=1)).strftime('%Y%m%d')
    if date_to is None:
        date_to = datetime.now().strftime('%Y%m%d')

    logger.info(f'ERP 고객출고 동기화 시작: {date_from} ~ {date_to}')

    # 1) 헤더 조회
    ok, headers, err = fetch_erp_outgoing_headers(date_from, date_to)
    if not ok:
        logger.error(f'ERP 고객출고 헤더 조회 실패: {err}')
        return 0, 0, 1, [f'헤더 조회 실패: {err}']
    if not headers:
        logger.info('ERP 고객출고 내역 없음')
        return 0, 0, 0, []

    # 2) 이미 동기화된 출고번호 목록
    existing_nbs = set(
        MaterialTransaction.objects.filter(
            transaction_type='OUT_ERP',
            erp_incoming_no__isnull=False
        ).values_list('erp_incoming_no', flat=True)
    )

    synced = 0
    skipped = 0
    errors = 0
    error_list = []

    for header in headers:
        isu_nb = header.get('isuNb', '')
        if not isu_nb:
            continue

        # SCM에서 올린 건 제외
        remark = header.get('remarkDc', '') or ''
        if 'SCM' in remark:
            skipped += 1
            continue

        # 헤더 단위 중복 체크 (existing_nbs에는 'IS...-순번' 형태이므로 prefix로 체크)
        if any(nb.startswith(isu_nb) for nb in existing_nbs):
            skipped += 1
            continue

        try:
            # 3) 디테일 조회
            ok2, details, err2 = fetch_erp_outgoing_details(isu_nb)
            if not ok2 or not details:
                skipped += 1
                continue

            isu_dt = header.get('isuDt', '')  # 'YYYYMMDD'
            wh_cd = header.get('whCd', '')    # 출고창고코드
            tr_cd = header.get('trCd', '')    # 거래처코드

            # 출고일 파싱
            try:
                erp_date = datetime.strptime(isu_dt, '%Y%m%d').date()
                now = tz.localtime(tz.now())
                isu_date = now.replace(year=erp_date.year, month=erp_date.month, day=erp_date.day)
            except (ValueError, TypeError):
                isu_date = tz.now()

            # 출고창고 매칭
            from_wh = Warehouse.objects.filter(code=wh_cd).first() if wh_cd else None
            if not from_wh:
                from_wh = Warehouse.objects.filter(code='2000').first()

            # 거래처 매칭
            from orders.models import Vendor
            vendor = Vendor.objects.filter(erp_code=tr_cd).first() if tr_cd else None
            if not vendor and tr_cd:
                vendor = Vendor.objects.filter(code=tr_cd).first()

            detail_synced = False

            # 4) 디테일별 트랜잭션 생성
            for detail in details:
                item_cd = detail.get('itemCd', '')
                if not item_cd:
                    continue

                isu_sq = detail.get('isuSq', 1)
                trx_key = f'{isu_nb}-{isu_sq}'
                if trx_key in existing_nbs:
                    continue

                # Part 매칭
                part = Part.objects.filter(part_no=item_cd).first()
                if not part:
                    continue

                qty = int(detail.get('isuQt', 0) or 0)
                if qty <= 0:
                    continue

                # MaterialStock 차감 (원자적)
                result_stock = 0
                if from_wh:
                    stock, _ = MaterialStock.objects.get_or_create(
                        warehouse=from_wh,
                        part=part,
                        lot_no=None,
                        defaults={'quantity': 0}
                    )
                    MaterialStock.objects.filter(id=stock.id).update(
                        quantity=Greatest(models.F('quantity') - qty, models.Value(0))
                    )
                    stock.refresh_from_db()
                    result_stock = stock.quantity

                detail_remark = detail.get('remarkDc', '') or ''
                wh_nm = header.get('whNm', '') or wh_cd
                tr_nm = header.get('attrNm', '') or tr_cd

                _create_trx(
                    transaction_type='OUT_ERP',
                    date=isu_date,
                    part=part,
                    lot_no=None,
                    quantity=-qty,
                    warehouse_from=from_wh,
                    result_stock=result_stock,
                    vendor=vendor,
                    remark=f'ERP출고({wh_nm}→{tr_nm}) {detail_remark}'.strip(),
                    erp_incoming_no=trx_key,
                    erp_sync_status='SUCCESS',
                    erp_sync_message=f'ERP 고객출고 동기화 ({isu_nb})',
                )
                existing_nbs.add(trx_key)
                detail_synced = True

            if detail_synced:
                synced += 1

        except Exception as e:
            errors += 1
            error_list.append(f'{isu_nb}: {str(e)}')
            logger.error(f'ERP 고객출고 동기화 오류 ({isu_nb}): {e}')

    logger.info(f'ERP 고객출고 동기화 완료: 신규 {synced}, 건너뜀 {skipped}, 오류 {errors}')
    return synced, skipped, errors, error_list
