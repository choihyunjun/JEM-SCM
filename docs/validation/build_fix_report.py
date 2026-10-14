"""Map every original error finding to passing regression evidence."""
import json
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent
audit = json.loads((HERE / 'audit-findings.json').read_text(encoding='utf-8'))
results = json.loads((HERE / 'fixes-results.json').read_text(encoding='utf-8'))
assert results['summary']['failures'] == results['summary']['errors'] == 0
changes = {
    'F01': '협력사 자사 발주 승인 권한을 등록·수정 권한과 분리했습니다. 단건·전체 승인 모두 업체 범위를 제한하고 POST/CSRF를 적용했습니다.',
    'F02': '저장 직전 원본 발주의 승인·마감 상태를 검증합니다. 빈 발주 ID로 우회해도 승인된 수기 발주 잔량이 없으면 차단합니다.',
    'F03': '같은 발주의 모든 LOT 수량을 합산합니다. 품번·발주 잠금 안에서 기존 발행량과 확인된 반출 복구량을 반영하여 서버에서 잔량을 검증합니다.',
    'F05': 'SNP·박스 수·총수량의 양수·정수·범위와 배열 길이, 날짜를 검증합니다. 하나라도 잘못되면 전체 등록을 중단합니다.',
    'F06': '납품서 번호에 UUID 식별자를 추가하여 같은 초에 등록되는 서로 다른 납품서 번호 충돌을 방지했습니다.',
    'A01': '납품서 출력·라벨 발행 시 모든 품목의 업체 소유권을 검사합니다. 타사/혼합 소유 납품서는 404로 차단하며 태그도 생성하지 않습니다.',
    'A02': '선택한 품번·발주·협력사가 모두 일치해야 저장됩니다. 잘못된 원본 발주 연결을 차단합니다.',
    'A03': 'LOT 재고 조회 API의 품목 검색에 협력사 범위를 적용했습니다.',
    'R06': '취소 대상 품목 FK에 연결된 발행 이력만 삭제합니다. 미사용 출력 태그는 취소 처리하며, 원본 연결이 없는 과거 이력은 자동 추정 삭제하지 않습니다.',
    'W04': '현재 DB 상태를 조건부 갱신하여 라벨을 선점합니다. 오래된 상태를 읽은 요청은 다시 차감할 수 없고, 재고 처리 실패 시 선점도 롤백됩니다.',
    'W05': '현장 이외 창고로 이동해도 사용 상태와 원본 이동 FK를 저장합니다. 중복 이동을 차단하고 취소 시 실제 도착 창고에서 복구하며 이후 재스캔이 가능합니다.',
    'E04': 'ERP 재고이동을 기존 영속 전송 대기열에 통합했습니다. 동일 이동은 하나의 요청으로 처리하고 타임아웃·결과 불명확 시 확인 대기 상태로 유지합니다.',
}
items = []
for row in audit['items']:
    if row['verdict'] != '오류':
        continue
    tests = []
    for old in row['evidence']:
        name = old['test'].rsplit('.', 1)[-1]
        matches = [t for t in results['tests'] if t['test'].endswith('.' + name)]
        assert matches and all(t['status'] == 'pass' for t in matches), name
        tests.extend(t['test'] for t in matches)
    assert tests, row['code']
    items.append(dict(code=row['code'], feature=row['feature'], expected=row['expected'],
        actual=changes[row['code']], evidence=tests, verdict='정상', priority=row['priority']))
assert len(items) == 12
table = ''.join('<tr>' + ''.join(f'<td>{escape(value)}</td>' for value in [
    item['code'] + ' · ' + item['feature'], item['expected'], item['actual'],
    '\n'.join(t.rsplit('.', 1)[-1] for t in item['evidence']), item['verdict'] + ' (격리 테스트)',
    item['priority'], '로컬 수정 완료 · 운영 반영 후 확인 필요']) + '</tr>' for item in items)
html = '''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SCM/WMS 오류 수정 결과</title><style>
*{box-sizing:border-box}body{margin:0;background:#f5f6f8;color:#27313c;font:14px/1.65 Pretendard,"Malgun Gothic",sans-serif}header{background:#fff;border-bottom:3px solid #900000;padding:24px 32px}h1{font-size:27px;margin:6px 0}main{padding:24px 32px}p{margin:8px 0}a{color:#900000}.box{padding:18px 22px;background:#fff;border:1px solid #dfe3e8;margin:0 0 20px}.numbers{display:flex;gap:32px;flex-wrap:wrap}.numbers strong{font-size:28px;display:block}.note{color:#667180;font-size:12px}.table-wrap{overflow:auto;background:#fff;border:1px solid #dfe3e8}table{border-collapse:collapse;min-width:1500px;width:100%;table-layout:fixed;font-size:12px}th,td{padding:12px;text-align:left;border-bottom:1px solid #dfe3e8;vertical-align:top;overflow-wrap:anywhere;white-space:pre-line}th{background:#edf0f4}th:nth-child(1){width:12%}th:nth-child(2){width:15%}th:nth-child(3){width:25%}th:nth-child(4){width:20%}th:nth-child(5){width:9%}th:nth-child(6){width:5%}th:nth-child(7){width:14%}h2{font-size:19px}code{font-size:12px}@media(max-width:700px){header,main{padding:20px 16px}.numbers{gap:18px}}@media print{@page{size:A3 landscape;margin:10mm}.table-wrap{overflow:visible}table{min-width:0}tr{break-inside:avoid}body{background:white}}
</style></head><body><header><div class="note">JEM · 2026-10-14 · 3959e98 이후 로컬 수정</div><h1>오류 12개 항목 수정 결과</h1><p>요청하신 오류 항목을 수정했습니다. 업무 기준 확인 필요 5개와 미검증 5개는 검토 대기 상태입니다.</p></header><main>
<section class="box numbers"><div><strong>12 / 12</strong>오류 항목 재검증 통과</div><div><strong>__TESTS__ / __TESTS__</strong>전체 회귀 테스트 통과</div><div><strong>63 / 63</strong>복사 DB 메뉴 응답 200</div></section>
<section class="box"><h2>적용 범위와 검증</h2><ul>
<li>수정 전 작업은 <code>3959e98</code>로 main에 커밋·푸시했습니다. 이 보고서는 이후 오류 수정과 재검증 결과입니다. 운영 서버 반영 여부는 배포 후 별도로 확인해야 합니다.</li>
<li>격리 DB와 합성 데이터로 기존 테스트 및 신규 43개 회귀 테스트를 실행했습니다. 외부 통신은 차단했으며 ERP 응답은 모의 처리했습니다.</li>
<li>로컬 DB를 읽기 전용으로 복사한 임시 DB에 마이그레이션을 적용했습니다. 메뉴 63개 응답 200, 테이블 104개 검사, 미적용 마이그레이션·누락 테이블·누락 열 모두 0입니다. 원본 DB는 변경하지 않았습니다.</li>
<li>새 마이그레이션 <code>material.0060_erp_stock_move_operation</code>은 ERP 요청 종류에 재고이동을 추가합니다. <code>makemigrations --check --dry-run</code>에서 추가 변경 없음이 확인됐습니다.</li>
<li>실제 운영 DB의 동시 부하, 운영 ERP 전표·스캐너·프린터는 미검증입니다. 정상 판정은 아래 재현 조건에 한정됩니다.</li>
</ul><p><a href="fixes-results.json">재검증 JSON</a> · <a href="fixes-test-log.txt">전체 실행 로그</a> · <a href="../../orders/test_workflow_fixes.py">업무 회귀 테스트</a> · <a href="../../material/test_stock_move_outbox.py">ERP 이동 회귀 테스트</a> · <a href="scm_workflow_audit.html">수정 전 검증 및 검토 대기 항목</a></p></section>
<h2>기능별 변경 결과</h2><div class="table-wrap"><table><thead><tr><th>기능</th><th>기대 동작</th><th>실제 동작</th><th>검증 근거</th><th>판정</th><th>중요도</th><th>개선안 / 후속</th></tr></thead><tbody>__ROWS__</tbody></table></div>
<section class="box" style="margin-top:24px"><h2>그대로 둔 업무 기준</h2><p>TAG 재스캔 자동 취소, FIFO 예외, 반출과 인수 확인의 분리, 사용된 재고의 정정 절차, 수기 발주 품번별 합산 방식은 변경하지 않았습니다.</p><p>과거 이력이 이미 잘못 연결되거나 ERP에 중복 반영된 데이터는 자동 정리하지 않았습니다. 이번 수정은 새 요청의 검증과 재발 방지입니다.</p></section>
</main></body></html>'''.replace('__TESTS__', str(results['summary']['tests'])).replace('__ROWS__', table)
(HERE / 'workflow_fixes.html').write_text(html, encoding='utf-8')
(HERE / 'fix-findings.json').write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding='utf-8')
print(f'Fixed findings: {len(items)}; passing regression tests: {results["summary"]["tests"]}')
