"""Regression coverage for the seven-document failure and cron overlap incident."""
import json
from contextlib import ExitStack
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from material.erp_detail_validation import require_details
from material.erp_sync_lock import ERPSyncBusy
from material.erp_sync_pipeline import sync_inventory
from material.models import MaterialStock, MaterialTransaction, StockSyncRun


class DetailResponseTests(SimpleTestCase):
    def test_error_and_empty_are_distinct_and_original_error_is_retained(self):
        with self.assertRaisesRegex(ValueError, 'DETAIL_API_FAILURE.*permission denied'):
            require_details(False, None, 'permission denied')
        with self.assertRaisesRegex(ValueError, 'DETAIL_EMPTY'):
            require_details(True, [], None)
        with self.assertRaisesRegex(ValueError, 'DETAIL_INVALID'):
            require_details(True, {'items': []}, None)
        rows = [{'itemCd': 'ZR700', 'rcvQt': '0.3337'}]
        self.assertIs(require_details(True, rows), rows)

    def test_diagnostic_is_read_only_and_preserves_raw_quantity(self):
        out = StringIO()
        with patch('material.erp_api.fetch_erp_incoming_detail', side_effect=[
            (False,None,'access denied'), (True,[],None),
            (True,[{'itemCd':'ZR700','rcvQt':'677.3337'}],None),
        ]) as fetch:
            call_command('diagnose_erp_documents', incoming=['RV1','RV2','RV3','RV3'],stdout=out)
        rows = json.loads(out.getvalue())['documents']
        self.assertEqual(fetch.call_count,3)
        self.assertIn('DETAIL_API_FAILURE',rows[0]['reason'])
        self.assertEqual(rows[0]['api_error'],'access denied')
        self.assertIn('DETAIL_EMPTY',rows[1]['reason'])
        self.assertEqual(rows[2]['items'][0]['rcvQt'],'677.3337')

    def test_diagnostic_accepts_outgoing_and_rejects_no_documents(self):
        with self.assertRaises(CommandError):
            call_command('diagnose_erp_documents',stdout=StringIO())
        out=StringIO()
        with patch('material.erp_api.fetch_erp_outgoing_details',return_value=(True,[],None)) as fetch:
            call_command('diagnose_erp_documents',outgoing=['IS1'],stdout=out)
        fetch.assert_called_once_with('IS1')
        self.assertIn('DETAIL_EMPTY',out.getvalue())

    def test_overlapping_commands_exit_normally_without_starting_jobs(self):
        for name in ('sync_erp_history','detect_past_changes'):
            with self.subTest(command=name), patch(
                f'material.management.commands.{name}.erp_sync_lock',side_effect=ERPSyncBusy('busy')
            ), patch(f'material.management.commands.{name}.Command._run') as run:
                out=StringIO()
                call_command(name,stdout=out)
                self.assertIn('건너뜀',out.getvalue())
                run.assert_not_called()

    @override_settings(ERP_ENABLED=True)
    def test_recent_command_uses_same_explicit_window_for_all_jobs(self):
        today=timezone.localdate()
        with patch('material.management.commands.sync_erp_auto.sync_inventory',return_value={
            'errors':0,'synced':0,'stock':{'adjusted':0}
        }) as sync:
            call_command('sync_erp_auto',days=7,stdout=StringIO())
            sync.assert_called_once_with(date_from=(today-timedelta(days=6)).strftime('%Y%m%d'),
                                         date_to=today.strftime('%Y%m%d'))
        with patch('material.management.commands.sync_erp_auto.sync_inventory') as sync:
            with self.assertRaises(CommandError):
                call_command('sync_erp_auto',days=0,stdout=StringIO())
            sync.assert_not_called()


class PersistentSyncFailureTests(TestCase):
    names=['incoming','issue','receipt','stock_transfer','adjustments','outgoing']

    def jobs(self,stack,failures=None):
        for name in self.names:
            errors=(failures or {}).get(name,[])
            stack.enter_context(patch(f'material.erp_api.sync_erp_{name}',return_value=(0,0,len(errors),errors)))
        return stack.enter_context(patch('material.erp_api.sync_stock_from_erp',return_value={
            'adjusted':1,'increased':0,'decreased':1,'error':None}))

    def history(self,**kwargs):
        today=timezone.localdate()
        call_command('sync_erp_history',date_from=today.replace(month=1,day=1).strftime('%Y%m%d'),
                     date_to=today.strftime('%Y%m%d'),stdout=StringIO(),stderr=StringIO(),**kwargs)

    def test_all_seven_errors_persist_but_erp_stock_is_corrected(self):
        errors={'incoming':[f'RV{i}: DETAIL_EMPTY' for i in range(5)],
                'outgoing':['IS1: DETAIL_API_FAILURE: timeout','IS2: DETAIL_EMPTY']}
        with ExitStack() as stack:
            stock=self.jobs(stack,errors)
            with self.assertRaisesRegex(CommandError,'현재고 보정 완료.*수불 이력 오류 7건'):
                self.history()
            stock.assert_called_once()
        run=StockSyncRun.objects.first()
        self.assertEqual(run.status, 'partial')
        self.assertEqual(run.summary['stock']['adjusted'], 1)
        self.assertEqual(run.summary['scope'],'full_history')
        self.assertEqual(len(run.summary['error_list']),7)
        self.assertEqual(len(run.summary['steps']),6)
        self.assertIn('timeout',run.summary['steps'][-1]['error_list'][0])

    def test_legacy_failure_does_not_block_erp_authoritative_stock(self):
        StockSyncRun.objects.create(status='failed',error='수불 오류 7건: 총량 보정을 보류했습니다.')
        StockSyncRun.objects.create(status='success',summary={'adjusted':1})
        with ExitStack() as stack:
            stock=self.jobs(stack)
            result=sync_inventory(date_from='20261013',date_to='20261019')
            stock.assert_called_once()
        self.assertEqual(result['errors'],0)
        self.assertTrue(StockSyncRun.objects.filter(status='failed').exists())

    def test_full_success_clears_marker_and_enables_recent_reconciliation(self):
        StockSyncRun.objects.create(status='failed',error='수불 오류 7건: 총량 보정을 보류했습니다.')
        with ExitStack() as stack:
            stock=self.jobs(stack)
            self.history()
            stock.assert_called_once()
            self.assertEqual(sync_inventory()['errors'],0)
            self.assertEqual(stock.call_count,2)

    def test_failed_full_correction_does_not_clear_marker(self):
        with ExitStack() as stack:
            stock=self.jobs(stack)
            stock.return_value={'adjusted':0,'error':'ERP timeout'}
            with self.assertRaisesRegex(CommandError,'ERP timeout'):
                self.history()
        self.assertEqual(StockSyncRun.objects.first().status, 'failed')
        self.assertEqual(StockSyncRun.objects.first().summary['stock_error'],'ERP timeout')

    def test_short_history_still_corrects_stock_after_prior_failure(self):
        StockSyncRun.objects.create(status='failed',error='수불 오류 7건: 총량 보정을 보류했습니다.')
        today=timezone.localdate().strftime('%Y%m%d')
        with ExitStack() as stack:
            stock=self.jobs(stack)
            call_command('sync_erp_history',date_from=today,date_to=today,stdout=StringIO(),stderr=StringIO())
            stock.assert_called_once()

    def test_importers_distinguish_empty_and_api_failure_without_stock_writes(self):
        from material.erp_api import sync_erp_incoming, sync_erp_outgoing
        cases=[(sync_erp_incoming,'incoming','rcvNb','RV1','fetch_erp_incoming_detail'),
               (sync_erp_outgoing,'outgoing','isuNb','IS1','fetch_erp_outgoing_details')]
        for importer,kind,key,number,detail in cases:
            for response,code in [((True,[],None),'DETAIL_EMPTY'),((False,None,'denied'),'DETAIL_API_FAILURE')]:
                with self.subTest(kind=kind,code=code), patch(
                    f'material.erp_api.fetch_erp_{kind}_headers',return_value=(True,[{key:number}],None)
                ), patch(f'material.erp_api.{detail}',return_value=response):
                    result=importer('20261001','20261019')
                self.assertEqual(result[2],1)
                self.assertIn(code,result[3][0])
        self.assertEqual(MaterialStock.objects.count(),0)
        self.assertEqual(MaterialTransaction.objects.count(),0)


class DocumentContextTests(TestCase):
    def diagnose(self, **kwargs):
        out = StringIO()
        call_command('diagnose_erp_documents', context=True, date_from='20260101',
                     date_to='20261019', stdout=out, **kwargs)
        return json.loads(out.getvalue())['documents']

    def test_context_matches_exact_document_and_line_keys_without_writes(self):
        from orders.models import Part
        from material.models import Warehouse
        part = Part.objects.create(part_no='ZR700', part_name='Resin')
        wh = Warehouse.objects.create(code='3000', name='Factory')
        for index, number in enumerate(['RV1', 'RV1-1', 'RV10-1']):
            MaterialTransaction.objects.create(transaction_no=f'T{index}', transaction_type='IN_ERP',
                                               part=part, warehouse_to=wh, quantity=25,
                                               erp_incoming_no=number)
        before = list(MaterialTransaction.objects.order_by('pk').values())
        with patch('material.erp_api.fetch_erp_incoming_headers', return_value=(True, [
            {'rcvNb': 'RV1', 'rcvDt': '20260601', 'whCd': '3000'}, {'rcvNb': 'RV10'},
        ], None)) as headers, patch('material.erp_api.fetch_erp_incoming_detail', return_value=(True, [], None)):
            rows = self.diagnose(incoming=['RV1', 'RV2', 'RV1'])
        headers.assert_called_once_with('20260101', '20261019')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['erp_header']['match_count'], 1)
        self.assertEqual(rows[0]['scm_transactions']['row_count'], 2)
        self.assertEqual(float(rows[0]['scm_transactions']['stored_quantity_sum']), 50)
        self.assertEqual(rows[0]['scm_transactions']['rows'][0]['part__part_no'], 'ZR700')
        self.assertEqual(rows[1]['erp_header']['status'], 'NOT_FOUND_IN_RANGE')
        self.assertEqual(rows[1]['scm_transactions']['row_count'], 0)
        self.assertEqual(rows[0]['status'], 'REVIEW')  # Context never clears an empty-detail failure.
        self.assertEqual(before, list(MaterialTransaction.objects.order_by('pk').values()))

    def test_header_failure_is_not_reported_as_missing_document(self):
        for response in [(False, None, 'denied'), (True, {'items': []}, None), (True, [None], None)]:
            with self.subTest(response=response), patch('material.erp_api.fetch_erp_outgoing_headers',
                    return_value=response), patch('material.erp_api.fetch_erp_outgoing_details',
                    return_value=(True, [], None)):
                rows = self.diagnose(outgoing=['IS1'])
            self.assertEqual(rows[0]['erp_header']['status'], 'UNVERIFIED')

    def test_invalid_context_dates_stop_before_api_calls(self):
        with patch('material.erp_api.fetch_erp_incoming_detail') as details, patch(
            'material.erp_api.fetch_erp_incoming_headers'
        ) as headers:
            for date_from, date_to in [(None, None), ('20261020', '20261019'),
                                      ('20260230', '20261019'), ('202611', '20261019')]:
                with self.subTest(date_from=date_from), self.assertRaises(CommandError):
                    call_command('diagnose_erp_documents', incoming=['RV1'], context=True,
                                 date_from=date_from, date_to=date_to, stdout=StringIO())
            details.assert_not_called()
            headers.assert_not_called()

    def test_api_phase_cannot_write_to_database(self):
        from django.db import connection
        def attempted_write(*args):
            with connection.cursor() as cursor:
                cursor.execute('DELETE FROM material_stocksyncrun')
        with patch('material.erp_api.fetch_erp_incoming_headers', side_effect=attempted_write), patch(
            'material.erp_api.fetch_erp_incoming_detail', side_effect=attempted_write
        ):
            rows = self.diagnose(incoming=['RV1'])
        self.assertEqual(rows[0]['erp_header']['status'], 'UNVERIFIED')
        self.assertEqual(rows[0]['status'], 'REVIEW')
        self.assertIn('SCM DB에 접근하지 않습니다', rows[0]['reason'])
        self.assertFalse(StockSyncRun.objects.exists())

    def test_local_phase_rejects_write_queries(self):
        from django.db import connection
        from material.management.commands.diagnose_erp_documents import Command
        def attempted_write():
            with connection.cursor() as cursor:
                cursor.execute('DELETE FROM material_stocksyncrun')
        with patch('django.db.models.query.QuerySet.count', side_effect=attempted_write):
            with self.assertRaisesRegex(RuntimeError, 'DB 변경'):
                Command().local_context('RV1')
        self.assertFalse(StockSyncRun.objects.exists())
