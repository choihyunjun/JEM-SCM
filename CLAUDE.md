# JEM SCM 작업 안내

진영전기 사내 시스템. 협력사 발주·납품(SCM), 자재·재고(WMS), 품질(QMS), 생산현장 지원을 한 Django 프로젝트에서 운영한다.
이 문서는 이 저장소에서 작업할 때 먼저 알아야 할 것만 적는다. 세부 이력은 git 기록과 `docs/`에 있다.

## 일하는 방식

- 답변과 보고는 모두 한국어로, 개발 용어를 풀어서 쓴다. 사용자는 개발자가 아니라 현업 담당자다.
- 코드 수정이 끝나면 따로 묻지 않고 수정한 파일만 커밋·푸시하고, 서버 배포 명령어와 마이그레이션 필요 여부를 알려 준다.
  - 기본: `git pull && sudo systemctl restart gunicorn`
  - 모델 변경 시: `git pull && python manage.py migrate && sudo systemctl restart gunicorn`
- 운영 서버 배포는 사용자가 "배포해줘"라고 할 때만 직접 한다. 절차는 아래 [배포](#배포).
- 디자인을 바꿀 때는 작업 파일만 고쳐 전후 캡처를 `design-preview/<이름>/`에 두고 먼저 보여 준 뒤, 승인받으면 커밋한다.
- 작업 전 `git status -sb`로 origin보다 뒤처지지 않았는지 확인한다. 작업 폴더는 이 폴더(`main`) 하나다.

## 건드리면 안 되는 것

- **생산현장 지원의 성형팀·금형팀 기능**: 사용자가 그 화면을 짚어 요청한 경우에만 고친다. 공용 모델·코드를 바꿀 때도 이 화면들에 영향이 없어야 한다.
- **의도된 동작** (버그처럼 보여도 고치지 않는다):
  - 'LOT 미지정' 재고는 대부분 정상이다. LOT은 SCM으로 입고·라벨 관리되는 품목에만 붙는다.
  - 입고 시 선입선출 위반은 경고만 한다. SCM 입고 처리는 직원만 한다.
  - 납품 가능 수량은 납품서 등록 수량 기준이다.
  - 수동출고는 ERP로 보내지 않는다. ERP에 따로 등록하지 않으면 다음 보정 때 'LOT 미지정'으로 되살아난다.
  - SCM 입고 현황에서 검사가 끝난 입고를 취소할 수 있는 동작은 기존 테스트가 고정하고 있다.
- 기존 화면의 디자인을 임의로 바꾸지 않는다. 새 기능은 그 화면이 이미 쓰는 서식과 부품을 재사용한다.

## 재고 규칙 (가장 자주 틀리는 부분)

- **재고 수량의 기준은 ERP(더존 아마란스)다.** SCM 수불 이력에 오류가 있어도 현재고 보정을 막지 않는다. ERP 음수 현재고도 그대로 반영한다(지정 LOT은 0까지, 나머지 음수는 미지정 버킷).
- 창고 `1000`, `8100`(수입검사 대기), `8200`(부적합)은 SCM 전용이라 ERP 보정·비교에서 뺀다 (`material/stock_reconciliation.py`의 `SCM_ONLY_WAREHOUSE_CODES`).
- 자재 흐름: `3200` 원재료창고 → `3000` 투입 → ERP 생산실적 입력 시 차감. 투입 창고는 `3000`, `4300`.
- 선입선출 순서: LOT 미지정 재고 먼저, 그다음 오래된 LOT. 재고 맞추기·ERP 이동 수신·이동요청 승인 모두 같다.
- 이전 LOT 납품은 등록할 때 막고, 내부 담당자가 1회에 한해 예외 승인한다.
- 수량 계산 규칙 (SQLite가 소수를 부정확하게 계산하기 때문):
  - 재고 수량을 DB에서 더하고 뺄 때는 `material/stock_safety.py`의 `quantity_change()`
  - 폼에서 받은 수량은 `material/quantities.py`의 `input_quantity()`
  - DB 합계 비교는 `stock_total()`
- 사람이 입력하는 수량(입고·수입검사·반출·이동요청)은 정수다. 소수는 ERP가 준 값에서만 생긴다.

## 구조

| 앱 | 주소 | 맡은 일 |
|---|---|---|
| `orders` | `/` | SCM: 발주, 납품서, 입고, 품목·거래처 마스터, 사용자·권한, 공용 뼈대(`base.html`) |
| `material` | `/wms/` | WMS: 재고, 입출고, 이동, 라벨, BOM, ERP 연동, 생산현장(성형·금형) |
| `qms` | `/qms/` | 품질: 수입검사, 부적합, 시정조치, 4M, ISIR, 계측기 등 |
| `admin_app` | `/system/` | 관리자 화면 |

- 화면 코드는 앱마다 `views.py` 한 파일에 몰려 있다(`material/views.py` 약 12,000줄, `orders/views.py` 약 4,700줄). 함수 이름으로 검색해서 찾는다.
- 새 기능은 `views.py`에 덧붙이지 말고 별도 모듈로 만든다. 이미 그렇게 나눈 예: `material/resin_yield.py`, `material/molding_report.py`, `material/late_defect.py`, `material/incoming.py`, `orders/deliveries.py`, `orders/returns.py`.
- ERP 연동은 `material/erp_api.py`(호출·동기화), `erp_outbox.py`(보낼 것 대기열), `erp_sync_pipeline.py`, `stock_reconciliation.py`(현재고 보정)에 있다. 주기 실행 명령은 `material/management/commands/`.
- 권한 검사: `menu_permission_required`(orders), `wms_permission_required`(material), `qms_permission_required`(qms).
- DB는 SQLite 한 파일이다. 운영 DB는 서버에만 있고 저장소에는 없다.

## 화면 서식

- 공용 테마: `orders/static/css/erp-theme.css`. 색은 여기 변수(`--erp-brand`, `--erp-ink`, `--erp-muted`, `--erp-line`, `--erp-warning`, `--erp-danger`, `--erp-success`)를 쓴다.
- 뼈대 템플릿: `orders/templates/base.html`, `material/.../material_base.html`, `production_base.html`, `qms/.../qms_base.html`. 뒤의 세 개에는 공용 `dtl-*` 서식(카드, 단추, 표, 알약 등)이 들어 있다.
- 공용 테마가 화면 너비에 따라 단추 최소 높이를 강제한다. 한 줄에 놓인 입력 칸과 단추 높이를 맞추려면 화면별 서식에서 `height`와 `min-height: 0`을 같이 지정한다.
- 템플릿은 줄바꿈 형식(CRLF/LF)이 파일마다 다르다. 스크립트로 고칠 때는 원래 형식을 유지한다.
- 색을 많이 걷어내는 정리는 사용자가 원하지 않는다. 글자 색은 본문/보조 두 단계로 맞추고, 지표 카드는 따로 떨어뜨려 둔다.

## 검증

- 테스트: `python manage.py test --settings=config.test_settings` (전체), 앱 하나만은 뒤에 `material` 등을 붙인다.
- 버그를 고치면 그 버그를 잡는 테스트를 하나 추가한다. 테스트 파일은 앱 폴더의 `test_*.py`.
- 화면을 바꿨으면 실제로 띄워서 본다. 운영 DB를 쓰지 말고, 임시 폴더에 DB 사본과 임시 설정(ERP 끔, 자동 로그인)을 두고 Playwright로 캡처한다. ERP를 부르는 화면은 호출 함수를 가짜로 바꿔서 띄운다.
- ERP 연동 구간은 모의 테스트로만 검증된다. 보고할 때 "실제 ERP로는 확인하지 못했다"고 분명히 적는다.

## 배포

서버 주소와 접속 방법은 저장소에 적지 않는다(작업자 메모리 참고). 서버 안에서의 순서:

1. `git pull --ff-only`
2. 모델이 바뀌었으면 DB 백업 후 `python manage.py migrate`
3. `python manage.py check_deployment` (테이블·칸·미적용 마이그레이션 점검)
4. `systemctl restart gunicorn` 후 로그인 화면이 200으로 응답하는지 확인

재시작 때 로그에 "Worker was sent SIGTERM" 한 줄이 남는 것은 정상이다.

## 저장소 정리 상태

- 최상위 폴더의 `check_*.py`, `zr_*.py`, `*_result.txt`, `investigate_*.py`, `project_all.py`, `project_full_code.txt` 등은 예전 조사용 임시 파일이다(git 미추적). 프로그램 동작과 무관하니 참고하지 말고, 새 임시 파일은 여기에 만들지 않는다.
- `design-preview/`는 디자인 전후 캡처 보관용이며 git에 올리지 않는다.
- `docs/`에는 과거 수정 기록과 협력사용 설명서가 있다.
