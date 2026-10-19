"""Keep ERP transport failures distinguishable from successful empty responses."""


def require_details(ok, details, error=None):
    if not ok:
        raise ValueError(f'[DETAIL_API_FAILURE] ERP 상세 API 실패: {error or "원인 미제공"}')
    if details is None or details == []:
        raise ValueError('[DETAIL_EMPTY] ERP 상세 API 성공, 상세 행 없음 (삭제/취소 여부 확인 필요)')
    if not isinstance(details, list) or any(not isinstance(row, dict) for row in details):
        raise ValueError('[DETAIL_INVALID] ERP 상세 응답 형식 오류: 객체 목록이 필요합니다.')
    return details
