"""Read specific ERP documents without importing or mutating local/ERP data."""
import json
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from material.erp_detail_validation import require_details


class Command(BaseCommand):
    help = 'ERP 전표 상세 읽기 전용 진단 (동기화·삭제·재고 변경 없음)'

    def add_arguments(self, parser):
        parser.add_argument('--incoming', nargs='+', default=[])
        parser.add_argument('--outgoing', nargs='+', default=[])

    def handle(self, *args, **options):
        from material.erp_api import fetch_erp_incoming_detail, fetch_erp_outgoing_details
        if not options['incoming'] and not options['outgoing']:
            raise CommandError('--incoming 또는 --outgoing에 전표번호를 지정하세요.')
        results = []

        def prohibit_sql(execute, sql, params, many, context):
            raise RuntimeError('읽기 전용 ERP 진단은 SCM DB에 접근하지 않습니다.')

        with connection.execute_wrapper(prohibit_sql):
            for kind, fetcher in [('incoming', fetch_erp_incoming_detail), ('outgoing', fetch_erp_outgoing_details)]:
                for number in dict.fromkeys(options[kind]):
                    result = {'type': kind, 'document': number}
                    try:
                        ok, details, error = fetcher(number)
                        result.update(api_success=ok, api_error=error,
                                      response_type=type(details).__name__,
                                      row_count=len(details) if isinstance(details,list) else None)
                        require_details(ok, details, error)
                        result['status'] = 'DETAIL_OK'
                        result['items'] = [{k:row.get(k) for k in ('itemCd','whCd','rcvSq','isuSq','rcvQt','isuQt')}
                                           for row in details]
                    except Exception as exc:
                        result['status'] = 'REVIEW'
                        result['reason'] = str(exc)
                    results.append(result)
        self.stdout.write(json.dumps({'read_only':True,'documents':results}, ensure_ascii=False, indent=2, default=str))
