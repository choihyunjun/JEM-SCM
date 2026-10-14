"""Build a standalone Korean report from recorded test evidence."""
from collections import Counter
from datetime import datetime
from html import escape as esc
import json
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
baseline = json.loads((HERE / 'baseline-results.json').read_text(encoding='utf-8'))
focused = json.loads((HERE / 'focused-results.json').read_text(encoding='utf-8'))
results = baseline['tests'] + focused['tests']
# Keep the original audit reproducible after fixes change source locations.
commit = 'c6083fb'
snapshot_commit = '3959e98'


def source_lines(file):
    return subprocess.check_output(['git', 'show', f'{snapshot_commit}:{file}'], cwd=ROOT).decode('utf-8').splitlines()
rows = []


def add(code, group, feature, expected, actual, verdict, priority, improvement, tests=(), sources=()):
    evidence = []
    for name in tests:
        matches = [record for record in results if record['test'].endswith('.' + name)]
        assert matches, name
        evidence.extend(matches)
    locations = []
    for file, symbol in sources:
        lines = source_lines(file)
        line = next(i for i, text in enumerate(lines, 1) if symbol in text)
        locations.append((file, line))
    rows.append(dict(code=code, group=group, feature=feature, expected=expected, actual=actual,
        verdict=verdict, priority=priority, improvement=improvement, evidence=evidence, sources=locations))


add('F01', '발주·납품', '협력사의 자사 발주 승인',
    '앞서 합의한 프로세스대로 협력사가 자신의 발주를 승인하고 납품서를 등록한다.',
    '승인 권한 플래그를 켠 협력사도 승인 요청에서 302로 돌아가며 approved_at이 비어 있다. 공급업체는 can_scm_order_edit 자체가 차단되고 승인 화면·뷰는 그 권한을 요구한다.',
    '오류', '높음', '발주 등록/수정 권한과 협력사 승인 권한을 분리하고 승인 대상의 업체 소유권을 검증한다. 문서만 수정하지 말고 실제 협력사 계정으로 재검증한다.',
    ['test_supplier_can_approve_own_order'], [('orders/views.py', 'def order_approve('), ('orders/access.py', 'VENDOR_BLOCKED')])
add('F02', '발주·납품', '승인 전·마감 발주 납품서 등록',
    '미승인·마감 발주는 목록뿐 아니라 저장 요청에서도 등록을 차단한다.',
    '목록은 승인·미마감 조건을 적용하지만, 직접 POST하면 미승인 발주와 마감 발주 각각 납품 품목 1건이 생성됐다.',
    '오류', '높음', '저장 트랜잭션 안에서 발주 승인·마감 상태를 재검증하고 잘못된 요청 전체를 쓰기 전에 거절한다.',
    ['test_unapproved_order_cannot_register_delivery', 'test_closed_order_cannot_register_delivery'],
    [('orders/views.py', 'def create_delivery_order('), ('orders/views.py', 'def label_list(')])
add('F03', '발주·납품', '발주 잔량 초과·다른 LOT 합산',
    '화면의 “납품가능 수량 초과 차단” 규칙을 서버에도 적용하고 같은 발주의 여러 LOT 수량을 합산한다.',
    '발주 100개에 101개 등록이 허용됐다. 같은 발주에 서로 다른 LOT 60개씩을 보내면 합계 120개가 등록됐다. 화면 JS는 개별 행만 잔량과 비교하며 최종 서버 합산 검증이 없다.',
    '오류', '높음', '발주별 신청 합계 + 기존 유효 발행량 − 확인된 반출 복구량으로 잔량을 검증한다. 동시 등록을 잠금/조건부 갱신으로 직렬화한다. 초과 허용이 필요하면 별도 승인 규칙을 명시한다.',
    ['test_delivery_must_not_exceed_remaining_order_quantity', 'test_multiple_lots_must_not_exceed_combined_remaining_quantity'],
    [('orders/templates/label_list.html', 'if (total > remain)'), ('orders/views.py', 'qty = int(snps[i])')])
add('F04', '발주·납품', '정상 분할 납품',
    '승인된 발주 100개를 40개·60개로 나눠 발행하면 잔량이 60개·0개로 변한다.',
    '40개 발행 후 목록 잔량 60개, 추가 60개 발행 후 해당 발주가 발행 대상 목록에서 제외됐다. 합계는 100개였다.',
    '정상', '높음', '정상 분할 경로는 유지하고 F02·F03의 서버 검증을 함께 적용한다.',
    ['test_partial_deliveries_reduce_remaining_quantity'])
add('F05', '발주·납품', 'SNP·포장 수 입력 검증',
    '각 값이 양의 정수인지 검증하고 잘못된 입력은 안내 메시지로 반환한다.',
    '숫자가 아닌 SNP는 HTTP 500을 반환했다. SNP -10 × 박스 수 -1은 양수 10개가 되어 잘못된 납품 품목이 생성됐다.',
    '오류', '높음', 'int 변환 예외와 각 인수의 양수·상한을 검증한다. 모든 행을 먼저 검증한 후 납품서와 발행 이력을 한 번에 저장한다.',
    ['test_non_numeric_packaging_is_rejected_without_server_error', 'test_negative_packaging_factors_are_rejected'],
    [('orders/views.py', 'qty = int(snps[i])')])
add('F06', '발주·납품', '동일 초 납품서 번호 충돌',
    '같은 초에 들어온 서로 유효한 등록 요청도 고유 납품서 번호를 받는다.',
    '같은 시각으로 고정한 연속 2회 요청 중 두 번째가 HTTP 500이었다. 번호가 초 단위 시각만 포함해 UNIQUE 제약에 충돌했다. 실제 병렬 부하 없이도 재현된다.',
    '오류', '높음', '번호 채번을 DB 시퀀스/UUID 또는 원자적 일련번호로 바꾸고 요청 재전송 키와는 별도로 관리한다.',
    ['test_two_valid_deliveries_in_same_second_both_succeed'], [('orders/views.py', 'order_no="DO-"')])

add('A01', '권한', '다른 업체 납품서·라벨 직접 출력',
    '목록에서 숨기는 것과 별개로, 직접 URL의 납품서 ID가 자사 것인지 확인한다.',
    '업체 A 계정이 업체 B의 납품서 출력 URL을 열면 HTTP 200이었다. 라벨 출력도 허용됐고 업체 B 품목의 ProcessTag까지 생성됐다.',
    '오류', '긴급', '납품서 조회·인쇄·태그 생성 전에 전체 품목의 업체 소유권을 검사하는 공통 조회 함수를 적용한다. 비권한 요청에서는 조회 결과와 쓰기가 모두 없어야 한다.',
    ['test_other_supplier_delivery_note_is_not_readable', 'test_other_supplier_labels_are_not_printable'],
    [('orders/views.py', 'def delivery_note_print('), ('orders/views.py', 'def label_print(')])
add('A02', '권한', '자사 품번에 타사 발주 연결',
    '품번과 연결 발주가 같은 업체·같은 품목에 속해야 한다.',
    '자사 품번을 전송하면서 order_ids에 다른 업체의 발주 ID를 지정하면 그 발주를 연결한 납품 품목이 생성됐다.',
    '오류', '긴급', '발주 조회를 업체·품번·승인 상태로 제한한다. 허용된 품번 검사만으로 연결 발주의 권한 검증을 대신하지 않는다.',
    ['test_other_supplier_order_cannot_be_linked_to_own_part'], [('orders/views.py', 'linked_order = Order.objects.get(id=order_ids[i])')])
add('A03', '권한', '타사 품목 LOT 재고 API',
    '협력사는 자사 품목의 LOT 재고만 조회한다.',
    '업체 A 계정으로 업체 B 품번의 LOT 상세 API를 호출했을 때 HTTP 200과 B 품목 재고 데이터가 반환됐다.',
    '오류', '긴급', '품목 조회에 업체 범위를 적용한다. 이 API 외 유사 조회·다운로드 엔드포인트도 동일 기준으로 점검한다.',
    ['test_other_supplier_lot_stock_is_not_readable'], [('orders/views.py', 'def get_lot_details(')])
add('A04', '권한', '타사 품번 등록·납품서 삭제 차단',
    '다른 업체의 품번으로 납품서를 만들거나 다른 업체 납품서를 삭제할 수 없다.',
    '타사 품번 등록 요청은 품목 생성 없이 거절됐다. 타사 납품서 삭제 요청 후 원본 납품서가 유지됐다.',
    '정상', '높음', '확인한 등록 품번·삭제 경로의 차단은 유지하되 A01·A02·A03에 같은 범위 검증을 확장한다.',
    ['test_other_supplier_part_is_rejected', 'test_other_supplier_delivery_cannot_be_deleted'])
add('A05', '권한', 'QMS 상세/수정·발주 엑셀 업체 범위',
    'QMS 부적합·클레임과 발주 엑셀은 업체별로 제한하고 조회 전용 사용자의 변경은 차단한다.',
    '타사 QMS 상세·쓰기 404, 읽기 전용 계정 수정 403, 업체 연결 없는 계정 차단, 협력사 발주 엑셀 범위 테스트가 통과했다. 모든 엔드포인트에 대한 결론은 아니다.',
    '정상', '높음', '이 공통 권한 패턴을 SCM 출력·LOT API에 재사용한다.',
    ['test_other_vendor_detail_and_write_are_not_found', 'test_readonly_account_can_view_but_cannot_save_detail_forms', 'test_vendor_excel_is_scoped_and_unlinked_vendor_is_empty'])

add('R01', '입고·검사·반출', '승인 발주 이후 입고→검사→부분 반출 연결',
    '검사 대상 100개는 대기창고에 들어가고, 판정 후 양품 80개·불량 20개로 분리된다.',
    '승인된 발주를 사전 데이터로 준비한 통합 요청에서 검사 전 대기 100/양품 0, 검사 후 대기 0/양품 80/불량 20을 확인했다. 반복 입고 요청은 1건만 반영됐다. 협력사 승인 단계는 F01 때문에 이 통과 범위에서 제외된다.',
    '정상', '높음', '재고 반영 시점과 창고 분리 구조를 유지한다. 승인 오류 수정 후 승인부터 이어지는 전체 재검증이 필요하다.',
    ['test_receipt_inspection_partial_return_and_acknowledgement_chain', 'test_different_lots_register_and_receive_both_quantities'])
add('R02', '입고·검사·반출', '양품·불량 합계와 재판정 중복',
    '양품·불량은 음수가 아니고 합계가 입고량과 같아야 한다. 반복 판정으로 재고가 늘면 안 된다.',
    '입고 100 기준 (110,-10), (-10,110), (90,0), 문자열 입력이 상태·재고 변경 없이 차단됐다. 80/20 판정 반복 시 한 번만 반영되고 전량 불량도 처리됐다.',
    '정상', '높음', '현재 검증을 유지한다. 실제 ERP 결과까지 일치하는지는 E06에서 별도 검증한다.',
    ['test_invalid_quantities_leave_everything_unchanged', 'test_batch_preserved_and_repeat_decision_does_not_duplicate', 'test_all_rejected_is_still_allowed'])
add('R03', '입고·검사·반출', '동시 수입검사 판정',
    '두 담당자가 같은 PENDING 상태를 읽어도 재고와 ERP 요청은 한 번만 반영한다.',
    '격리 SQLite에서 두 스레드가 같은 대기 상태를 읽도록 동기화한 테스트가 통과했다. ERP 함수 모의 호출 1회, 대상 재고와 이동 이력 1회가 확인됐다.',
    '정상', '높음', '해당 시나리오의 보호는 유지한다. 운영 DB 종류·Gunicorn 다중 프로세스 검증은 W09와 구분한다.',
    ['test_two_simultaneous_decisions_only_apply_once'])
add('R04', '입고·검사·반출', '부분 반출·인수 확인·발주 잔량 복구',
    '실물 반출량만 부적합 재고에서 차감하고, 인수 확인한 수량만 원본 발주 잔량으로 복구한다.',
    '불량 20 중 5개 반출 시 불량 재고 15, 확인 전 복구 0, 확인 후 복구 5였다. 다른 발주 순번에 복구되지 않았고 중복 확인은 0개 추가 반영됐다.',
    '정상', '높음', '실물 반출과 업체 인수 확인의 분리 상태를 화면에서 명확하게 보여준다.',
    ['test_receipt_inspection_partial_return_and_acknowledgement_chain', 'test_partial_shipment_restores_only_exact_order_sequence', 'test_repeat_shipment_and_ack_are_idempotent'])
add('R05', '입고·검사·반출', '입고 취소 범위·ERP 실패·전량 불량',
    '선택한 입고만 취소하고 다른 납품서·LOT를 보존한다. ERP 삭제 실패 시 로컬 기록을 보존한다.',
    '선택 입고와 관련 품목만 취소, 다른 납품서 보존, ERP 실패 시 로컬 기록 유지, 전량 불량의 중복 재입고 차단 테스트가 통과했다.',
    '정상', '높음', '현재 원본 연결 및 취소 원자성을 유지한다. 납품서 자체 취소의 별도 문제는 R06에 명시했다.',
    ['test_scm_item_cancel_keeps_other_part_and_other_delivery', 'test_erp_failure_does_not_delete_local_records', 'test_all_bad_delivery_remains_received_and_cannot_be_rescanned'])
add('R06', '입고·검사·반출', '미입고 납품서 취소의 발행 이력 삭제 범위',
    '취소 대상 납품 품목에 연결된 LabelPrintLog만 삭제하고 다른 납품서의 잔량 계산 근거는 보존한다.',
    '같은 날·같은 품번·같은 수량의 납품서 2건 중 1건을 취소하자 다른 납품서는 남았지만 그 발행 이력도 삭제됐다. 삭제 조건이 품번·수량·일자이고 delivery_item FK를 사용하지 않는다.',
    '오류', '긴급', '삭제 범위를 delivery_item 또는 delivery_item__order로 한정한다. 기존 취소 이력에 같은 조건의 타 납품서가 있는지는 별도 읽기 전용 정합성 점검을 계획한다.',
    ['test_cancel_delivery_keeps_other_delivery_label_history'], [('orders/views.py', 'def delete_delivery_order(')])
add('R07', '입고·검사·반출', '판정 정정·이미 사용/반출된 LOT 보호',
    '정정 사유를 요구하고 이미 사용·반출된 LOT는 단순 취소로 되돌리지 않는다.',
    '정정 후 재판정의 중복 방지, 사유 없는 정정 차단, 반출 후 정정 차단, 재고를 다시 채운 경우에도 기존 사용 이력에 따른 차단이 통과했다.',
    '정상', '높음', '보호 장치는 유지하되 차단된 사용자가 다음에 처리할 담당자·대체 절차를 안내한다.',
    ['test_correction_restores_stock_summary_and_redecision_does_not_duplicate', 'test_correction_requires_reason_and_blocks_after_shipment', 'test_correction_blocks_used_lot_even_when_stock_was_replenished'])

add('W01', 'WMS', 'LOT·생산 배치별 재고 분리',
    '같은 날짜라도 생산 배치가 다르면 별도 재고로 취급하고 다른 배치를 임의 차감하지 않는다.',
    '배치별 고유 재고, 선택 stock_id의 품목·출발창고 일치, 다른 배치 잔량 차용 차단, ERP 재입고 중복 건너뛰기 테스트가 통과했다.',
    '정상', '높음', '수량 변경 경로마다 stock_id·LOT·production_lot 식별을 유지한다.',
    ['test_different_batches_remain_separate', 'test_stock_must_belong_to_selected_part_and_source', 'test_selected_batch_shortage_cannot_borrow_other_batch', 'test_same_date_batches_stay_separate_and_duplicate_receipt_is_skipped'])
add('W02', 'WMS', '창고 이동·FIFO 자동 분할·승인 취소',
    '선택 LOT만 이동하고 FIFO 선택 시 여러 배치로 분할한다. 실패하면 차감·증가·이력을 함께 되돌린다.',
    '배치 이동과 같은 배치 복구, FIFO 분할, 여러 줄의 동일 재고 초과 사용 시 전체 롤백, 이력 저장 실패 후 재시도 중복 방지 테스트가 통과했다.',
    '정상', '높음', '기존 원자적 처리와 조건부 차감을 유지한다.',
    ['test_selected_batch_moves_and_revoke_restores_same_batch', 'test_fifo_still_splits_by_production_batch', 'test_multiple_lines_cannot_overdraw_same_stock_and_roll_back_together', 'test_history_failure_rolls_back_batch_and_mirror_then_retry_counts_once'])
add('W03', 'WMS', '원재료 라벨 순차 중복 스캔·FIFO',
    '3000 현장에 사용한 RM 라벨을 재스캔하면 차단하고, 더 오래된 LOT 재고가 있으면 신규 LOT 투입을 막는다.',
    '100개 중 25개 투입 후 순차 재스캔은 거절되고 출발 75/도착 25가 유지됐다. 더 오래된 LOT 10개가 있으면 이동 이력 없이 투입이 차단됐다.',
    '정상', '높음', '아래 W04·W05의 다른 요청 순서·도착지에서도 같은 안전 조건을 적용한다.',
    ['test_raw_label_sequential_duplicate_scan_is_blocked', 'test_raw_label_older_lot_blocks_newer_lot'])
add('W04', 'WMS', '오래된 라벨 상태로 중복 스캔',
    '두 요청이 모두 INSTOCK을 읽었더라도 같은 라벨의 재고 이동은 한 번만 성공해야 한다.',
    '첫 스캔 전에 읽은 라벨 객체를 두 번째 요청에 제공하는 재현에서 출발 재고가 100→75→50으로 두 번 차감됐다. 이는 오래된 읽기 시뮬레이션이며 실제 운영 DB 병렬 부하 테스트는 아니다.',
    '오류', '높음', '라벨 상태 전이를 트랜잭션 안의 조건부 갱신 또는 행 잠금으로 확보한 뒤 재고를 이동한다. 이동 건에도 라벨+요청 고유 식별자를 둔다.',
    ['test_stale_label_read_should_not_allow_second_stock_move'], [('material/views.py', 'def api_process_tag_scan(')])
add('W05', 'WMS', '3000 이외 창고로 같은 라벨 반복 이동',
    '같은 실물 라벨을 동일 목적지로 재전송해도 출발 재고가 반복 차감되지 않는다.',
    '동일 RM 라벨을 2000 창고로 두 번 스캔하면 두 번 성공하여 출발 재고가 100→50이 됐다. USED 전이는 3000 목적지에만 적용된다.',
    '오류', '높음', '사용 여부와 현재 위치/이동 처리 이력을 분리해 추적하고, 중복 요청은 기존 결과를 반환한다.',
    ['test_raw_label_nonproduction_repeat_move_should_be_blocked'], [('material/views.py', "mark_used = (target_warehouse_code == '3000')")])
add('W06', 'WMS', '현품표 재스캔으로 자동 투입 취소',
    '반복 스캔과 명시적 취소를 구분할지 업무 기준을 정한다. 원재료 라벨과 일관된 조작이 바람직하다.',
    'ProcessTag는 두 번째 스캔 시 별도 취소 버튼 없이 앞선 이동을 반대로 처리했다. 출발 재고가 100→75→100으로 복구됐다. 반면 RM 라벨의 두 번째 스캔은 차단된다.',
    '업무 기준 확인 필요', '높음', '자동 취소가 의도된 정책인지 확인한다. 권장은 재스캔 차단 + 이력에서 명시적 취소이며, 연속 스캐너 입력 실수로 재고가 원복되지 않도록 한다.',
    ['test_process_tag_repeat_scan_should_not_silently_cancel'], [('material/views.py', "if tag.status == 'USED':")])
add('W07', 'WMS', 'FIFO 적용 범위와 예외',
    '입고 경고·현장 투입 차단·수동 LOT 선택에서 FIFO 예외를 누구에게 허용하는지 일관되게 정의한다.',
    'SCM 입고는 최근 LOT가 먼저 존재하면 경고를 남기는 구조이고, RM 스캔은 오래된 LOT가 남으면 차단한다. 이동 요청에는 FIFO 자동 분할과 특정 LOT 선택이 모두 있다.',
    '업무 기준 확인 필요', '중간', '입고 순서와 출고 순서의 FIFO 의미를 구분하고, 수동 LOT 예외의 권한·사유·기록 정책을 합의한다.',
    [], [('orders/views.py', 'def receive_delivery_order_confirm('), ('material/views.py', 'def transfer_request_approve('), ('material/erp_api.py', 'def fifo_sort_key(')])
add('W08', 'WMS', '실운영 동시 처리·다중 작업자 재고 일관성',
    '운영과 동일 DB 및 여러 Gunicorn 프로세스에서 스캔·입고·취소·ERP 동기화가 겹쳐도 수량이 일치한다.',
    '이번에는 SQLite 격리 테스트와 지정된 두 스레드 검사 시나리오, 오래된 읽기 재현만 수행했다. 운영 DB 종류·잠금·타임아웃·다중 호스트 부하는 검증하지 않았다.',
    '미검증', '높음', '운영 DB 복제 검증 환경에서 동시 스캔/입고/취소/동기화 경쟁 시나리오를 별도로 수행한다.')

add('E01', 'ERP 연동', '입고 요청의 커밋·재시도 보호',
    '로컬 커밋 전 ERP 쓰기를 하지 않고 같은 입고 요청은 중복 전송하지 않는다.',
    '커밋 전 미전송, 롤백 시 요청 소멸, 성공 후 중복 요청 재사용, 미전송 취소, 삭제 후 재등록 순서 및 이력 보존 테스트가 통과했다.',
    '정상', '높음', '입고 outbox 패턴을 유지하고 재고이동 전송에도 확대한다.',
    ['test_no_remote_call_before_commit_and_rollback_discards_request', 'test_committed_registration_is_sent_once_and_duplicate_call_returns_existing', 'test_replacement_waits_for_delete_success'], [('material/erp_outbox.py', 'def enqueue_registration(')])
add('E02', 'ERP 연동', '입고 타임아웃·불명확 결과',
    '타임아웃을 미처리로 단정하지 않고 ERP 조회로 실제 처리 여부를 확인한다.',
    '타임아웃은 REVIEW로 남고 자동 재전송되지 않았다. 마커 조회로 완료를 확인하고 불완전 조회는 재전송을 허용하지 않는 테스트가 통과했다. ERP 응답은 모의 응답이다.',
    '정상', '높음', '운영 담당자에게 “ERP 확인 필요”와 확인·재시도 절차를 안내한다.',
    ['test_timeout_is_review_and_never_automatically_resent', 'test_timeout_can_be_reconciled_by_marker_without_resending', 'test_incomplete_lookup_does_not_enable_resend'])
add('E03', 'ERP 연동', 'ERP 가져오기 재실행·부분 실패',
    '이미 반영한 전표/행은 건너뛰고 실패한 행만 반영한다. 조회 실패를 재고 0으로 처리하지 않는다.',
    '부분 전표의 누락 행만 재시도, 이동/입고 이력 실패 시 롤백, 잘못된·실패한 재고 응답에서 기존 재고 보존, 동기화 중 변경된 재고의 덮어쓰기 방지 테스트가 통과했다.',
    '정상', '높음', '운영 ERP 데이터 형식 변화와 삭제 전표 처리에 대한 별도 계약 테스트를 추가한다.',
    ['test_partial_document_retry_imports_missing_line_only', 'test_purchase_receipt_history_failure_rolls_back_and_retry_counts_once', 'test_bad_empty_failed_and_negative_responses_do_not_erase_stock', 'test_stock_changed_during_erp_request_is_not_overwritten'])
add('E04', 'ERP 연동', '재고이동 쓰기 재호출 중복',
    '같은 로컬 재고이동 거래가 이미 성공했다면 다시 ERP 쓰기 요청을 보내지 않는다.',
    'register_erp_stock_move를 동일 거래로 2회 호출하면 성공 전표번호가 있어도 ERP 쓰기 함수가 2회 호출됐다. 실제 ERP가 두 전표를 생성하는지는 외부 시스템 검증 전이다.',
    '오류', '높음', '성공/진행/불명확 상태를 구분하는 이동 outbox와 고유 요청 키를 적용한다. 타임아웃 후 무조건 재전송하지 않는다.',
    ['test_same_erp_stock_move_is_not_sent_twice'], [('material/erp_api.py', 'def register_erp_stock_move(')])
add('E05', 'ERP 연동', '동기화 잠금의 다중 서버 적용',
    '여러 애플리케이션 서버에서도 같은 동기화가 겹치지 않는다.',
    '현재 OS 파일 잠금은 단일 호스트를 전제로 한다. 같은 스레드 재진입·다른 스레드 차단 테스트는 통과했지만, 여러 서버가 같은 잠금 파일을 공유하는지는 확인하지 않았다.',
    '미검증', '높음', '실제 배포가 단일 서버인지 확인하고 다중 서버면 DB/공유 잠금으로 검증한다.',
    ['test_nested_same_thread_allowed_other_thread_rejected_then_released'], [('material/erp_sync_lock.py', 'def erp_sync_lock(')])
add('E06', 'ERP 연동', '실제 ERP 장애·응답 유실·전표 대사',
    'ERP가 처리 후 응답만 잃은 상황에서도 전표·재고·로컬 상태를 복구한다.',
    '실제 ERP, 운영 네트워크, 운영 DB에는 연결하지 않았다. ERP 내부의 멱등성 및 전표 결과는 이번 테스트만으로 입증할 수 없다.',
    '미검증', '높음', 'ERP 검증 환경에서 처리 후 응답 유실, 지연, 중복 요청, 로컬 결과 저장 실패 후 대사를 재현한다.')

add('U01', '사용성', '반출 완료와 협력사 인수 확인의 두 단계',
    '실물 반출과 상대 업체 수령 확인이 각각 필요한지, 납품 잔량 복구 시점을 합의한다.',
    '현재 반출 시 부적합 재고가 차감되고 업체 인수 확인 시 납품 가능 수량이 복구된다. 인수 확인이 늦으면 실제 반출 후에도 재납품 가능 수량이 늦게 열린다.',
    '업무 기준 확인 필요', '중간', '단계를 임의 삭제하지 말고 담당자·처리 기한·미확인 알림을 정한다. 반출 즉시 복구 정책을 원하면 중복 확인 보호와 대사를 함께 설계한다.',
    ['test_partial_shipment_restores_only_exact_order_sequence'], [('orders/returns.py', 'def restored_quantities(')])
add('U02', '사용성', '이미 사용/반출된 입고의 취소 경로',
    '기록 보존을 지키면서 사용자가 다음 처리 절차를 이해할 수 있어야 한다.',
    '이미 이동·사용·반출된 건은 단순 취소/정정이 차단된다. 보호는 타당하지만 메시지 이후 실제 담당자 인계와 보정 절차의 사용성은 업무 확인이 필요하다.',
    '업무 기준 확인 필요', '중간', '차단 사유와 관련 이동·반출 이력을 링크하고 담당자에게 요청할 보정 절차를 안내한다. 임의 강제 취소는 추가하지 않는다.',
    ['test_correction_requires_reason_and_blocks_after_shipment', 'test_correction_blocks_used_lot_even_when_stock_was_replenished'])
add('U03', '사용성', '수기 발주의 품번별 합산 표시',
    '수기 발주도 발주별 추적이 필요한지, 품번 합산 잔량 방식이 업무에 맞는지 합의한다.',
    'ERP 발주는 발주/순번별로, 수기 발주는 품번별로 합산하여 납품 가능량을 계산한다. 수기 납품 항목은 발주 연결 없이 생성 가능한 구조다.',
    '업무 기준 확인 필요', '중간', '발주별 납기·단가·취소 추적이 필요하면 수기도 원본 발주 연결을 유지한다. 합산이 의도라면 사용자에게 기준을 명시한다.',
    [], [('orders/views.py', 'manual_orders = order_q.filter(')])
add('U04', '사용성', '실사용 입력 부담·프린터·스캐너',
    '실제 협력사와 현장 담당자가 주요 작업을 이해하고 반복 입력 없이 수행한다.',
    '이번 검증은 코드와 Django 요청/응답 중심이다. 실제 사용자 과제 수행 시간, 스캐너 연속 입력, 라벨 프린터 위치, 모바일 장치에서의 업무 완주는 측정하지 않았다.',
    '미검증', '중간', '협력사 1명·입고 담당자 1명과 분할 납품, 부분 불량, 반출, 취소 과제를 수행하며 클릭 수·재입력·오류 복구 시간을 기록한다.')
add('D01', '배포', '운영 DB 복구 적용 상태',
    '현재 코드에 필요한 마이그레이션·테이블·열이 운영 DB와 일치한다.',
    '앞선 서버 출력에서 note 열 누락을 확인해 c6083fb 복구 마이그레이션을 제공했다. 이번 검증의 DB는 모델로 새로 만든 SQLite이며, 운영 서버 재배포 후 PASS 결과는 아직 받지 않았다.',
    '미검증', '높음', '서버의 check_deployment PASS와 Gunicorn active를 확인하고 실제 오류 화면을 다시 조회한다. 테스트 통과를 운영 장애 해결로 해석하지 않는다.')


counts = Counter(row['verdict'] for row in rows)
status_classes = {'정상': 'ok', '오류': 'bad', '업무 기준 확인 필요': 'policy', '미검증': 'unknown'}


def anchor(test):
    return 'test-' + test.replace('.', '-')


def evidence_html(row):
    links = []
    for test in row['evidence']:
        label = test['test'].split('.')[-1]
        links.append(f'<a href="#{anchor(test["test"])}"><code>{esc(label)}</code> · {esc(test["status"])}</a>')
    for file, line in row['sources']:
        links.append(f'<a href="../../{esc(file)}"><code>{esc(file)}:{line}</code></a>')
    return '<br>'.join(links) or '범위 밖 / 후속 환경 검증 필요'


table = []
for row in rows:
    table.append(f'''<tr data-verdict="{row['verdict']}" data-group="{row['group']}" data-priority="{row['priority']}">
<th scope="row"><span class="id">{row['code']} · {row['group']}</span>{esc(row['feature'])}</th>
<td>{esc(row['expected'])}</td><td>{esc(row['actual'])}</td>
<td class="evidence">{evidence_html(row)}</td>
<td><span class="status {status_classes[row['verdict']]}">{row['verdict']}</span></td>
<td><strong class="priority-{row['priority']}">{row['priority']}</strong></td>
<td>{esc(row['improvement'])}</td></tr>''')

used = {record['test'] for row in rows for record in row['evidence']}
details = []
for record in results:
    if record['test'] not in used:
        continue
    detail = record.get('detail', '격리 테스트에서 해당 assert 검증이 통과했습니다. 자세한 입력/검증은 테스트 소스를 확인하세요.')
    module, cls, method = record['test'].rsplit('.', 2)
    file = module.replace('.', '/') + '.py'
    lines = source_lines(file)
    line = next(i for i, text in enumerate(lines, 1) if 'def ' + method + '(' in text)
    details.append(f'''<details id="{anchor(record['test'])}"><summary><span class="status {'ok' if record['status']=='pass' else 'bad'}">{record['status']}</span> {esc(record['test'])}</summary>
<p><a href="../../{file}">{file}:{line}</a></p><pre>{esc(detail)}</pre></details>''')

groups = ''.join(f'<option>{esc(group)}</option>' for group in dict.fromkeys(row['group'] for row in rows))
cards = ''.join(f'<div><strong>{counts[status]}</strong><span>{status}</span></div>' for status in status_classes)
html = '''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>SCM/WMS 업무 흐름 검증 보고서</title><style>
:root{--brand:#900000;--ink:#242c35;--muted:#657080;--line:#dfe3e8}*{box-sizing:border-box}body{margin:0;background:#f5f6f8;color:var(--ink);font:14px/1.65 Pretendard,"Malgun Gothic",sans-serif}header{background:#fff;border-bottom:3px solid var(--brand);padding:28px 32px}main{padding:24px 32px;max-width:1900px;margin:auto}h1{margin:6px 0;font-size:29px;letter-spacing:-1px}h2{font-size:20px;margin:30px 0 12px}h3{font-size:16px;margin:18px 0 8px}p{margin:8px 0}a{color:#8b0000;text-underline-offset:3px}.eyebrow,.meta{color:var(--muted);font-size:12px}.lead{font-size:16px;max-width:1100px}.callout{border-left:4px solid var(--brand);background:#fff;padding:16px 20px;margin:18px 0}.stats{display:grid;grid-template-columns:repeat(4,1fr);border:1px solid var(--line);background:white}.stats div{padding:16px 20px;border-right:1px solid var(--line)}.stats div:last-child{border:0}.stats strong{display:block;font-size:30px;line-height:1.3}.stats span{color:var(--muted);font-size:13px}.scope,.plan{background:white;border:1px solid var(--line);padding:18px 22px}.scope ul,.plan ol{margin:6px 0;padding-left:22px}.flow{display:flex;flex-wrap:wrap;gap:8px;margin:16px 0}.flow span{border:1px solid var(--line);background:white;padding:8px 12px}.flow b{align-self:center;color:#8b929b}.tools{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:14px 0}select,input,button{font:inherit;font-size:13px;border:1px solid #c9d0d8;border-radius:4px;min-height:36px;padding:6px 10px;background:white}input{min-width:220px}button{cursor:pointer}button:hover{background:#f0f2f5}.table-wrap{overflow:auto;background:#fff;border:1px solid var(--line);max-height:78vh}table{border-collapse:collapse;width:100%;min-width:1480px;table-layout:fixed;font-size:12px}thead th{position:sticky;top:0;background:#edf0f4;z-index:2;text-align:left;padding:12px 10px;border-bottom:1px solid #cad1da}td,tbody th{padding:14px 10px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line);overflow-wrap:anywhere;font-weight:400}tbody th{font-weight:600}tbody tr:hover{background:#fafbfc}.id{display:block;font-size:11px;font-weight:500;color:var(--muted);margin-bottom:6px}.status{display:inline-block;border-radius:4px;padding:3px 7px;line-height:1.5;font-size:11px;font-weight:600}.ok{color:#276443;background:#eaf4ed}.bad{color:#a32735;background:#fbecef}.policy{color:#865914;background:#fff3d8}.unknown{color:#596572;background:#edf0f3}.priority-긴급{color:#a32735}.priority-높음{color:#875824}.evidence{font-size:11px;line-height:1.65}.evidence a{display:inline-block;margin-bottom:7px}code{font-family:Consolas,monospace;font-size:.92em}.note{font-size:12px;color:var(--muted)}details{background:white;border:1px solid var(--line);padding:10px 14px;margin:6px 0;scroll-margin-top:12px}summary{cursor:pointer;overflow-wrap:anywhere}details:target{border-color:var(--brand)}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;line-height:1.6;background:#f4f5f7;padding:12px}.evidence-list{margin-top:24px}.hidden{display:none}footer{padding:26px 32px;color:var(--muted);font-size:12px}@media(max-width:700px){header,main{padding:20px 16px}h1{font-size:23px}.stats{grid-template-columns:repeat(2,1fr)}.stats div{border-bottom:1px solid var(--line)}.tools>*{max-width:100%}.table-wrap{max-height:70vh}}@media print{@page{size:A3 landscape;margin:12mm}body{background:white;font-size:11px}header,main{padding:8px}.tools,.evidence-list,footer{display:none}.table-wrap{overflow:visible;max-height:none}table{min-width:0;font-size:10px}thead{display:table-header-group}thead th{position:static}tr{break-inside:avoid}.hidden{display:table-row}.stats strong{font-size:22px}}
</style></head><body><header><div class="eyebrow">JEM · SCM / QMS / WMS · WORKFLOW REVIEW</div><h1>업무 흐름 검증 보고서</h1>
<div class="meta">검토일 2026-10-14 · 기준 커밋 __COMMIT__ · 업무 코드 수정 없이 검증 · 합성 데이터</div>
<p class="lead">검사·반출의 수량 처리와 기존 회귀 경로는 대체로 보호되어 있지만, 협력사 접근 범위와 납품서 등록·취소, 스캔 중복 처리에 수정이 필요한 경로가 확인됐습니다.</p></header>
<main><div class="callout"><strong>협력사 확대 운영 전에 먼저 처리할 항목</strong><p>A01–A03 타사 자료 접근·발주 연결, R06 취소 시 다른 납품서 발행 이력 삭제를 최우선으로 수정하십시오. 이어서 F01–F03 승인·잔량 검증과 W04–W05 / E04 중복 처리 보호를 보강하는 순서를 권장합니다.</p></div>
<div class="stats">__CARDS__</div><p class="note">위 숫자는 검증 표의 항목 수입니다. 테스트 수나 독립된 결함 개수와 다릅니다.</p>
<h2>검증 범위와 근거</h2><div class="scope"><ul>
<li>기존 회귀 테스트 <strong>190개 통과</strong>. 신규 표적 시나리오 <strong>23개 중 6개 통과, 17개 기대 조건 미충족</strong>. 실행 오류·건너뜀은 0개입니다.</li>
<li>17개 미충족에는 한 원인의 여러 재현과 업무 정책 확인 대상이 포함됩니다. 단순히 “오류 17개”로 집계하지 않았습니다.</li>
<li>Django 테스트 클라이언트/기존 직접 뷰 테스트, 소스 분석, 격리 SQLite 메모리 DB를 사용했습니다. ERP는 비활성화 또는 모의 응답이며 실제 외부 네트워크 연결은 차단했습니다.</li>
<li>이번 테스트는 모델 기준으로 테이블을 생성합니다. 과거 마이그레이션 전체 적용, 운영 DB, 실제 ERP, 실제 프린터·스캐너 및 전체 사용자 동시 부하는 검증 범위 밖입니다.</li>
<li>“정상”은 기재된 조건에서만 확인한 결과입니다. 전체 업무나 운영 장애 해결을 보증하지 않습니다. 협력사 승인 단계는 오류로 확인했으며, 수량 흐름 통합 테스트는 승인된 발주 데이터를 준비한 뒤 시작했습니다.</li>
<li>기준 커밋은 __COMMIT__입니다. 기존 미커밋 UI 변경 2개 파일(소요 현황 템플릿/CSS)은 유지했습니다. 이번 작업에서 운영 기능·DB·API는 수정하지 않았습니다.</li>
</ul><p><a href="baseline-results.json">기존 테스트 결과 JSON</a> · <a href="baseline-test-log.txt">기존 실행 로그</a> · <a href="focused-results.json">표적 테스트 결과 JSON</a> · <a href="focused-test-log.txt">표적 실행 로그</a> · <a href="workflow_cases.py">재현 코드</a></p></div>
<div class="flow"><span>발주 승인 · 보완 필요</span><b>→</b><span>납품서 등록 · 서버 검증 보완</span><b>→</b><span>입고 · 대기 재고</span><b>→</b><span>검사 · 양품/불량 분리</span><b>→</b><span>부적합 반출 · 실물 차감</span><b>→</b><span>인수 확인 · 발주 잔량 복구</span></div>
<h2>기능별 판정</h2><p class="note">중요도: 긴급 = 타사 정보 노출·타 거래 훼손 / 높음 = 수량·거래·운영 오류 / 중간 = 정책·사용성 확인. “업무 기준 확인 필요”는 확인된 동작의 의도·승인 기준이 필요한 항목입니다.</p>
<div class="tools"><label>판정 <select id="verdict"><option value="">전체 판정</option><option>오류</option><option>정상</option><option>업무 기준 확인 필요</option><option>미검증</option></select></label>
<label>영역 <select id="group"><option value="">전체 영역</option>__GROUPS__</select></label><label>중요도 <select id="priority"><option value="">전체 중요도</option><option>긴급</option><option>높음</option><option>중간</option></select></label>
<input id="search" type="search" aria-label="검증 항목 검색" placeholder="기능·근거·개선안 검색"><button id="reset" type="button">초기화</button><button type="button" id="print">인쇄 / PDF</button><span id="count" aria-live="polite"></span></div>
<div class="table-wrap"><table><colgroup><col style="width:13%"><col style="width:13%"><col style="width:22%"><col style="width:17%"><col style="width:8%"><col style="width:5%"><col style="width:22%"></colgroup>
<thead><tr><th>기능</th><th>기대 동작</th><th>실제 동작</th><th>검증 근거</th><th>판정</th><th>중요도</th><th>개선안</th></tr></thead><tbody>__ROWS__</tbody></table></div>
<h2>개선 순서</h2><div class="plan"><ol>
<li><strong>권한과 타 거래 보존:</strong> 납품서 출력/태그 발행/LOT API 업체 범위, 발주 연결 검증, 납품서 취소의 발행 이력 삭제 범위를 먼저 수정합니다.</li>
<li><strong>등록 단계 검증:</strong> 협력사 승인 권한 분리, 승인·마감·합산 잔량·포장 수 검증, 충돌 없는 납품서 번호를 적용합니다.</li>
<li><strong>중복과 재시도:</strong> 라벨 상태의 원자적 선점, 창고별 이동 이력, ERP 이동 outbox를 적용합니다.</li>
<li><strong>업무 합의:</strong> TAG 재스캔 자동 취소, 인수 확인 후 잔량 복구, 수기 발주 합산, FIFO 예외를 결정합니다.</li>
<li><strong>최종 검증:</strong> 수정 후 표적 실패를 재검증하고 기존 회귀 테스트를 실행합니다. 이후 운영과 동일한 검증 DB에서 실제 동시 처리, ERP 대사, 사용자 과제 수행을 확인합니다.</li>
</ol></div>
<h2>다시 실행하는 방법</h2><pre>python docs/validation/run_workflow_audit.py baseline
python docs/validation/run_workflow_audit.py focused
python docs/validation/build_workflow_report.py</pre><p class="note">focused는 현재 확인된 안전 조건 미충족 때문에 종료 코드 1을 반환합니다. 일부러 정상 처리하지 않았습니다. 일반 test*.py 검색에서는 제외되며 명시적으로 실행합니다. 실행 결과가 바뀌면 보고서의 서술·판정도 다시 검토해야 합니다.</p>
<section class="evidence-list"><h2>개별 테스트 근거</h2><p class="note">표의 검증 근거를 누르면 해당 항목으로 이동합니다. pass는 기재된 입력과 assert에 한정됩니다.</p>__DETAILS__</section>
</main><footer>검증 산출물 · 추가 MCP/외부 서비스 설치 없음 · 외부 API 쓰기 없음 · 운영 데이터 변경 없음</footer>
<script>
const rows=[...document.querySelectorAll('tbody tr')],controls=['verdict','group','priority','search'].map(id=>document.getElementById(id));
function applyFilters(){const [v,g,p,s]=controls.map(e=>e.value.trim());let count=0;for(const row of rows){const visible=(!v||row.dataset.verdict===v)&&(!g||row.dataset.group===g)&&(!p||row.dataset.priority===p)&&(!s||row.textContent.toLowerCase().includes(s.toLowerCase()));row.classList.toggle('hidden',!visible);if(visible)count++;}document.getElementById('count').textContent=count+' / '+rows.length+'항목';}
controls.forEach(e=>e.addEventListener('input',applyFilters));document.getElementById('reset').onclick=()=>{controls.forEach(e=>e.value='');applyFilters();};document.getElementById('print').onclick=()=>window.print();
document.querySelectorAll('a[href^="#test-"]').forEach(a=>a.addEventListener('click',()=>{const target=document.getElementById(a.hash.slice(1));if(target)target.open=true;}));applyFilters();
</script></body></html>'''
for key, value in {'COMMIT': commit, 'CARDS': cards, 'ROWS': ''.join(table), 'DETAILS': ''.join(details), 'GROUPS': groups}.items():
    html = html.replace('__' + key + '__', value)
if (HERE / 'workflow_fixes.html').exists():
    html = html.replace('<main>', '<main><div class="callout"><strong>이 표는 수정 전 검증 기록입니다.</strong><p>오류 12개 항목의 수정 및 재검증 결과는 <a href="workflow_fixes.html">오류 수정 결과 보고서</a>에서 확인하세요. 업무 기준 확인 필요·미검증 항목은 검토 대기 상태로 유지했습니다.</p></div>', 1)
(HERE / 'scm_workflow_audit.html').write_text(html, encoding='utf-8')
(HERE / 'audit-findings.json').write_text(json.dumps({'commit': commit, 'counts': dict(counts), 'items': rows}, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'items': len(rows), 'verdicts': dict(counts)}, ensure_ascii=False))
