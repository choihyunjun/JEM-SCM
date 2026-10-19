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
from material.erp_sync_pipeline import sync_inventory, unresolved_full_history_failure
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

    def test_all_seven_details_persist_and_stock_is_not_corrected(self):
        errors={'incoming':[f'RV{i}: DETAIL_EMPTY' for i in range(5)],
                'outgoing':['IS1: DETAIL_API_FAILURE: timeout','IS2: DETAIL_EMPTY']}
        with ExitStack() as stack:
            stock=self.jobs(stack,errors)
            with self.assertRaisesRegex(CommandError,'수불 오류 7건'):
                self.history()
            stock.assert_not_called()
        run=StockSyncRun.objects.first()
        self.assertEqual(run.summary['scope'],'full_history')
        self.assertEqual(len(run.summary['error_list']),7)
        self.assertEqual(len(run.summary['steps']),6)
        self.assertIn('timeout',run.summary['steps'][-1]['error_list'][0])

    def test_recent_success_cannot_hide_legacy_full_failure(self):
        StockSyncRun.objects.create(status='failed',error='수불 오류 7건: 총량 보정을 보류했습니다.')
        StockSyncRun.objects.create(status='success',summary={'adjusted':1})
        with ExitStack() as stack:
            stock=self.jobs(stack)
            result=sync_inventory(date_from='20261013',date_to='20261019')
            stock.assert_not_called()
        self.assertEqual(result['errors'],1)
        self.assertIn('미해결',result['error_list'][0])
        self.assertIsNotNone(unresolved_full_history_failure())

    def test_full_success_clears_marker_and_enables_recent_reconciliation(self):
        StockSyncRun.objects.create(status='failed',error='수불 오류 7건: 총량 보정을 보류했습니다.')
        with ExitStack() as stack:
            stock=self.jobs(stack)
            self.history()
            stock.assert_called_once()
            self.assertIsNone(unresolved_full_history_failure())
            self.assertEqual(sync_inventory()['errors'],0)
            self.assertEqual(stock.call_count,2)

    def test_failed_full_correction_does_not_clear_marker(self):
        with ExitStack() as stack:
            stock=self.jobs(stack)
            stock.return_value={'adjusted':0,'error':'ERP timeout'}
            with self.assertRaisesRegex(CommandError,'ERP timeout'):
                self.history()
        self.assertIsNotNone(unresolved_full_history_failure())
        self.assertEqual(StockSyncRun.objects.first().summary['stock_error'],'ERP timeout')

    def test_short_history_run_also_respects_prior_full_failure(self):
        StockSyncRun.objects.create(status='failed',error='수불 오류 7건: 총량 보정을 보류했습니다.')
        today=timezone.localdate().strftime('%Y%m%d')
        with ExitStack() as stack:
            stock=self.jobs(stack)
            with self.assertRaisesRegex(CommandError,'미해결'):
                call_command('sync_erp_history',date_from=today,date_to=today,stdout=StringIO(),stderr=StringIO())
            stock.assert_not_called()

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
