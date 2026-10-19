"""Read specific ERP documents without importing or mutating local/ERP data."""
import json
from datetime import datetime
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from material.erp_detail_validation import require_details


class Command(BaseCommand):
    help = 'ERP 전표 상세 읽기 전용 진단 (동기화·삭제·재고 변경 없음)'

    def add_arguments(self, parser):
        parser.add_argument('--incoming', nargs='+', default=[])
        parser.add_argument('--outgoing', nargs='+', default=[])
        parser.add_argument('--context', action='store_true', help='ERP 목록 정보와 SCM 수불 기록도 읽기 전용 조회')
        parser.add_argument('--from', dest='date_from')
        parser.add_argument('--to', dest='date_to')

    def header_context(self, fetcher, numbers, number_key, date_from, date_to):
        """An empty list is evidence only for the explicitly queried date range."""
        try:
            ok, headers, error = fetcher(date_from, date_to)
            base = {'api_success': ok, 'api_error': error,
                    'date_from': date_from, 'date_to': date_to}
            if not ok or not isinstance(headers, list) or any(not isinstance(row, dict) for row in headers):
                return {number: {**base, 'status': 'UNVERIFIED'} for number in numbers}
            result = {}
            for number in numbers:
                matches = [row for row in headers if row.get(number_key) == number]
                # Include the selected documents only, never the full supplier listing.
                result[number] = {**base, 'status': 'FOUND' if matches else 'NOT_FOUND_IN_RANGE',
                                  'match_count': len(matches), 'rows': matches}
            return result
        except Exception as exc:
            return {number: {'status': 'UNVERIFIED', 'reason': str(exc)} for number in numbers}

    def local_context(self, number):
        from django.db.models import Q, Sum
        from material.models import MaterialTransaction

        def select_only(execute, sql, params, many, context):
            if not sql.lstrip().upper().startswith('SELECT '):
                raise RuntimeError('진단 중 SCM DB 변경은 허용하지 않습니다.')
            return execute(sql, params, many, context)

        with connection.execute_wrapper(select_only):
            rows = MaterialTransaction.objects.filter(
                Q(erp_incoming_no=number) | Q(erp_incoming_no__startswith=number + '-')
            )
            count = rows.count()
            return {'row_count': count, 'stored_quantity_sum': rows.aggregate(total=Sum('quantity'))['total'],
                    'truncated': count > 50,
                    'rows': list(rows.order_by('id').values(
                        'id', 'transaction_no', 'transaction_type', 'date', 'erp_incoming_no',
                        'part__part_no', 'quantity', 'warehouse_from__code', 'warehouse_to__code',
                    )[:50])}

    def handle(self, *args, **options):
        from material.erp_api import fetch_erp_incoming_detail, fetch_erp_outgoing_details
        if not options['incoming'] and not options['outgoing']:
            raise CommandError('--incoming 또는 --outgoing에 전표번호를 지정하세요.')
        if options['context']:
            try:
                dates = [datetime.strptime(options[key] or '', '%Y%m%d').date()
                         for key in ('date_from', 'date_to')]
                if dates[0] > dates[1] or any(len(options[key]) != 8 for key in ('date_from', 'date_to')):
                    raise ValueError
            except (ValueError, TypeError):
                raise CommandError('--context에는 --from YYYYMMDD --to YYYYMMDD 조회 기간이 필요합니다.')
        results = []

        def prohibit_sql(execute, sql, params, many, context):
            raise RuntimeError('읽기 전용 ERP 진단은 SCM DB에 접근하지 않습니다.')

        with connection.execute_wrapper(prohibit_sql):
            for kind, fetcher in [('incoming', fetch_erp_incoming_detail), ('outgoing', fetch_erp_outgoing_details)]:
                headers = {}
                if options['context'] and options[kind]:
                    from material.erp_api import fetch_erp_incoming_headers, fetch_erp_outgoing_headers
                    header_fetcher = fetch_erp_incoming_headers if kind == 'incoming' else fetch_erp_outgoing_headers
                    headers = self.header_context(header_fetcher, options[kind],
                                                  'rcvNb' if kind == 'incoming' else 'isuNb',
                                                  options['date_from'], options['date_to'])
                for number in dict.fromkeys(options[kind]):
                    result = {'type': kind, 'document': number}
                    if options['context']:
                        result['erp_header'] = headers[number]
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
        if options['context']:
            for result in results:
                try:
                    result['scm_transactions'] = self.local_context(result['document'])
                except Exception as exc:
                    result['scm_transactions'] = {'status': 'UNVERIFIED', 'reason': str(exc)}
        self.stdout.write(json.dumps({'read_only':True,'documents':results}, ensure_ascii=False, indent=2, default=str))
