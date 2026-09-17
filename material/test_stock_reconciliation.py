import json
import tempfile
import threading
from contextlib import ExitStack
from datetime import date
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.cache import cache
from django.db import transaction
from django.db.models import Sum
from django.test import RequestFactory, TestCase, SimpleTestCase, override_settings
from django.utils import timezone

from orders.models import Part
from .erp_api import sync_stock_from_erp, sync_erp_stock_transfer, sync_erp_incoming
from .erp_sync_lock import erp_sync_lock, ERPSyncBusy
from .erp_sync_pipeline import sync_inventory
from .models import MaterialStock, MaterialTransaction, StockSyncRun, Warehouse
from .stock_safety import deduct_stock
from .views import get_lot_details, manual_outgoing


class StockReconciliationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.part = Part.objects.create(part_no='P1', part_name='Test')
        self.wh = Warehouse.objects.create(code='4300', name='Factory')
        self.user = User.objects.create(username='stock-test', is_superuser=True)

    def stock(self, qty, lot=None, batch=None):
        return MaterialStock.objects.create(warehouse=self.wh, part=self.part,
                                            quantity=qty, lot_no=lot, production_lot=batch)

    def row(self, qty, **kwargs):
        return dict(whCd=self.wh.code, itemCd=self.part.part_no, invQt1=qty, **kwargs)

    def sync(self, rows):
        with patch('material.erp_api.fetch_erp_stock', return_value=(True, rows, None)):
            return sync_stock_from_erp()

    def total(self):
        return MaterialStock.objects.aggregate(q=Sum('quantity'))['q']

    def request(self, method='get', data=None):
        request = getattr(RequestFactory(), method)('/', data or {})
        request.user = self.user
        request.session = {}
        request._messages = FallbackStorage(request)
        return request

    def test_negative_lots_are_visible_and_sum_matches_same_snapshot(self):
        self.stock(35716)
        self.stock(-33, date(2026, 6, 20))
        self.stock(-3, date(2026, 6, 21))
        result = json.loads(get_lot_details(self.request(), 'P1').content)
        self.assertEqual(result['total_quantity'], 35680)
        self.assertEqual(sum(row['quantity'] for row in result['lot_details']), 35680)
        self.assertEqual(result['negative_count'], 2)
        self.assertFalse(result['sync_needed'])
        self.assertFalse(result['fifo_warning'])

    def test_live_case_normalizes_without_changing_total_and_is_idempotent(self):
        bucket = self.stock(35716)
        a = self.stock(-33, date(2026, 6, 20))
        b = self.stock(-3, date(2026, 6, 21))
        result = self.sync([self.row(35680)])
        self.assertIsNone(result['error'])
        self.assertEqual(result['negative_normalized'], 2)
        bucket.refresh_from_db(); a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual((bucket.quantity, a.quantity, b.quantity), (35680, 0, 0))
        self.assertEqual(self.total(), 35680)
        self.assertEqual(MaterialTransaction.objects.count(), 4)
        self.assertEqual(MaterialTransaction.objects.aggregate(q=Sum('quantity'))['q'], 0)
        self.assertEqual(self.sync([self.row(35680)])['adjusted'], 0)
        self.assertEqual(MaterialTransaction.objects.count(), 4)
        self.assertEqual(StockSyncRun.objects.first().status, 'success')

    def test_undated_batches_are_trimmed_individually_not_overdrawn(self):
        a = self.stock(30, batch='2633511')
        b = self.stock(70, batch='2634511')
        self.sync([self.row(20)])
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual((a.quantity, b.quantity), (0, 20))
        self.assertFalse(MaterialStock.objects.filter(quantity__lt=0).exists())
        self.assertEqual(self.total(), 20)

    def test_fifo_reduces_oldest_lot_and_keeps_recent_lot(self):
        old = self.stock(50, date(2026, 1, 1))
        recent = self.stock(70, date(2026, 2, 1))
        self.stock(20)
        self.sync([self.row(90)])
        old.refresh_from_db(); recent.refresh_from_db()
        self.assertEqual((old.quantity, recent.quantity, self.total()), (20, 70, 90))

    def test_negative_unassigned_balance_is_absorbed_without_changing_total(self):
        lot = self.stock(50, date(2026, 1, 1))
        bucket = self.stock(-20)
        self.sync([self.row(30)])
        lot.refresh_from_db(); bucket.refresh_from_db()
        self.assertEqual((lot.quantity, bucket.quantity, self.total()), (30, 0, 30))

    def test_invalid_row_after_valid_row_prevents_all_corrections(self):
        stock = self.stock(100)
        result = self.sync([self.row(20), dict(whCd='4300', itemCd='OTHER')])
        self.assertTrue(result['error'])
        stock.refresh_from_db()
        self.assertEqual(stock.quantity, 100)
        self.assertFalse(MaterialTransaction.objects.exists())

    def test_missing_balance_preserves_stock_explicit_zero_clears(self):
        stock = self.stock(25)
        other = Part.objects.create(part_no='P2', part_name='Other')
        result = self.sync([dict(whCd='4300', itemCd=other.part_no, invQt1=0)])
        stock.refresh_from_db()
        self.assertEqual(stock.quantity, 25)
        self.assertEqual(result['skipped_missing'], 1)
        self.assertEqual(StockSyncRun.objects.first().status, 'partial')
        self.sync([self.row(0)])
        stock.refresh_from_db()
        self.assertEqual(stock.quantity, 0)

    def test_bad_empty_failed_and_negative_responses_do_not_erase_stock(self):
        stock = self.stock(25)
        for rows in ([], [self.row('bad')], [dict(whCd='4300', itemCd='P1')], [self.row('NaN')], [self.row(-1)]):
            with self.subTest(rows=rows):
                self.assertTrue(self.sync(rows)['error'])
                stock.refresh_from_db()
                self.assertEqual(stock.quantity, 25)
        with patch('material.erp_api.fetch_erp_stock', return_value=(False, None, 'timeout')):
            self.assertEqual(sync_stock_from_erp()['error'], 'timeout')
        self.assertFalse(MaterialTransaction.objects.exists())

    def test_duplicate_erp_rows_sum_instead_of_last_row_winning(self):
        self.stock(0)
        self.assertIsNone(self.sync([self.row(12), self.row(8)])['error'])
        self.assertEqual(self.total(), 20)

    def test_stock_changed_during_erp_request_is_not_overwritten(self):
        stock = self.stock(100)
        def fetch(**kwargs):
            MaterialStock.objects.filter(pk=stock.pk).update(quantity=70)
            return True, [self.row(90)], None
        with patch('material.erp_api.fetch_erp_stock', side_effect=fetch):
            result = sync_stock_from_erp()
        self.assertEqual(result['skipped_changed'], 1)
        stock.refresh_from_db()
        self.assertEqual(stock.quantity, 70)
        self.assertFalse(MaterialTransaction.objects.exists())

    def test_history_failure_rolls_back_stock_and_negative_normalization(self):
        self.stock(110)
        self.stock(-10, date(2026, 1, 1))
        before = list(MaterialStock.objects.values_list('pk', 'quantity'))
        with patch('material.erp_api._create_trx', side_effect=RuntimeError('history failed')):
            result = self.sync([self.row(80)])
        self.assertTrue(result['error'])
        self.assertEqual(list(MaterialStock.objects.values_list('pk', 'quantity')), before)
        self.assertEqual(StockSyncRun.objects.first().status, 'failed')

    def test_stale_debit_is_rejected_and_counterpart_rolls_back(self):
        stock = self.stock(100)
        MaterialStock.objects.filter(pk=stock.pk).update(quantity=30)
        with self.assertRaises(ValueError), transaction.atomic():
            self.stock(70, date(2026, 1, 1))
            deduct_stock(stock, 70)
        self.assertEqual(MaterialStock.objects.count(), 1)
        self.assertEqual(self.total(), 30)

    def test_manual_outgoing_shortage_creates_no_false_history(self):
        stock = self.stock(20)
        response = manual_outgoing(self.request('post', {
            'warehouse_id': self.wh.pk, 'part_ids[]': [self.part.pk],
            'stock_ids[]': [stock.pk], 'quantities[]': [30],
            'date': '2026-09-17',
        }))
        self.assertEqual(response.status_code, 302)
        stock.refresh_from_db()
        self.assertEqual(stock.quantity, 20)
        self.assertFalse(MaterialTransaction.objects.exists())

    def test_valid_manual_outgoing_still_debits_and_records_full_quantity(self):
        stock = self.stock(20)
        manual_outgoing(self.request('post', {
            'warehouse_id': self.wh.pk, 'part_ids[]': [self.part.pk],
            'stock_ids[]': [stock.pk], 'quantities[]': [10], 'date': '2026-09-17',
        }))
        self.assertEqual(MaterialTransaction.objects.get().quantity, -10)
        self.assertEqual(self.total(), 10)


class ERPTransferSafetyTests(TestCase):
    def setUp(self):
        self.part = Part.objects.create(part_no='P1', part_name='Test')
        self.source = Warehouse.objects.create(code='4200', name='Source')
        self.target = Warehouse.objects.create(code='4300', name='Target')
        self.stock = MaterialStock.objects.create(warehouse=self.source, part=self.part,
            lot_no=date(2026, 6, 20), quantity=80)
        self.details = [dict(itemCd='P1', moveSq=1, moveQt=100, fwhCd='4200', twhCd='4300')]

    def sync(self):
        with patch('material.erp_api.fetch_erp_transfer_headers', return_value=(True, [dict(moveNb='MOVE', moveDt='20260917')], None)), \
             patch('material.erp_api.fetch_erp_transfer_details', return_value=(True, self.details, None)):
            return sync_erp_stock_transfer()

    def test_erp_shortage_uses_unassigned_only_and_retry_is_idempotent(self):
        self.assertEqual(self.sync()[:3], (1, 0, 0))
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, 0)
        self.assertEqual(MaterialStock.objects.get(warehouse=self.source, lot_no=None).quantity, -20)
        self.assertEqual(MaterialStock.objects.filter(warehouse=self.target).aggregate(q=Sum('quantity'))['q'], 100)
        self.sync()
        self.assertEqual(MaterialTransaction.objects.count(), 1)

    def test_history_failure_rolls_back_transfer_then_retry_succeeds(self):
        with patch('material.erp_api._create_trx', side_effect=RuntimeError('fail')), self.assertLogs('material.erp_api', level='ERROR'):
            self.assertEqual(self.sync()[2], 1)
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, 80)
        self.assertEqual(MaterialStock.objects.count(), 1)
        self.assertEqual(self.sync()[2], 0)

    def test_partial_document_retry_imports_missing_line_only(self):
        self.sync()
        self.details.append(dict(self.details[0], moveSq=2, moveQt=5))
        self.sync()
        self.assertEqual(MaterialTransaction.objects.count(), 2)
        self.assertEqual(MaterialStock.objects.get(warehouse=self.source, lot_no=None).quantity, -25)

    def test_unknown_warehouse_does_not_fall_back_to_2000(self):
        self.details[0]['fwhCd'] = 'MISSING'
        with self.assertLogs('material.erp_api', level='ERROR'):
            self.assertEqual(self.sync()[2], 1)
        self.assertEqual(MaterialStock.objects.count(), 1)
        self.assertFalse(MaterialTransaction.objects.exists())

    def test_deleted_used_erp_receipt_does_not_overdraw_original_lot(self):
        self.stock.quantity = 20
        self.stock.save()
        MaterialTransaction.objects.create(transaction_no='OLD', transaction_type='IN_ERP',
            date=timezone.now(), part=self.part, quantity=100, lot_no=self.stock.lot_no,
            warehouse_to=self.source, erp_incoming_no='DELETED-1')
        # A different valid header establishes a nonempty response for the period.
        with patch('material.erp_api.fetch_erp_incoming_headers', return_value=(True, [dict(rcvNb='OTHER', remarkDc='SCM')], None)):
            sync_erp_incoming(timezone.localdate().strftime('%Y%m%d'), timezone.localdate().strftime('%Y%m%d'))
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.quantity, 20)
        self.assertFalse(MaterialTransaction.objects.filter(transaction_no='OLD').exists())
        self.assertEqual(MaterialTransaction.objects.get().quantity, 0)
        self.assertIn('DELETED-1', MaterialTransaction.objects.get().remark)

    def test_purchase_receipt_history_failure_rolls_back_and_retry_counts_once(self):
        headers = [dict(rcvNb='PURCHASE', rcvDt='20260917', whCd='4200')]
        details = [dict(itemCd='P1', rcvSq=1, rcvQt=10)]
        with patch('material.erp_api.fetch_erp_incoming_headers', return_value=(True, headers, None)), \
             patch('material.erp_api.fetch_erp_incoming_detail', return_value=(True, details, None)):
            with patch('material.erp_api._create_trx', side_effect=RuntimeError('fail')), self.assertLogs('material.erp_api', level='ERROR'):
                self.assertEqual(sync_erp_incoming()[2], 1)
            self.assertEqual(MaterialStock.objects.count(), 1)
            self.assertFalse(MaterialTransaction.objects.exists())
            self.assertEqual(sync_erp_incoming()[0], 1)
            self.assertEqual(sync_erp_incoming()[0], 0)
            self.assertEqual(MaterialStock.objects.get(lot_no=None).quantity, 10)
            self.assertEqual(MaterialTransaction.objects.count(), 1)


class SyncPipelineTests(TestCase):
    names = ['incoming', 'issue', 'receipt', 'stock_transfer', 'adjustments', 'outgoing']

    def test_total_reconciliation_runs_after_all_imports(self):
        calls = []
        with ExitStack() as stack:
            for name in self.names:
                stack.enter_context(patch(f'material.erp_api.sync_erp_{name}', side_effect=lambda name=name: (calls.append(name) or (0, 0, 0, []))))
            stock = stack.enter_context(patch('material.erp_api.sync_stock_from_erp', side_effect=lambda: (calls.append('stock') or {'adjusted': 0, 'error': None})))
            self.assertEqual(sync_inventory()['errors'], 0)
        self.assertEqual(calls, self.names + ['stock'])
        stock.assert_called_once()

    def test_failed_import_defers_total_correction_and_preserves_success_time(self):
        previous = StockSyncRun.objects.create(status='success', finished_at=timezone.now())
        with ExitStack() as stack:
            for name in self.names:
                stack.enter_context(patch(f'material.erp_api.sync_erp_{name}', return_value=(0, 0, 1, ['timeout']) if name == 'issue' else (0, 0, 0, [])))
            stock = stack.enter_context(patch('material.erp_api.sync_stock_from_erp'))
            self.assertEqual(sync_inventory()['errors'], 1)
        stock.assert_not_called()
        self.assertEqual(StockSyncRun.objects.first().status, 'failed')
        self.assertEqual(StockSyncRun.objects.filter(status='success').get().pk, previous.pk)
        self.assertTrue(cache.get('erp_sync_progress')['error'])


class ERPLockTests(SimpleTestCase):
    def test_nested_same_thread_allowed_other_thread_rejected_then_released(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(ERP_SYNC_LOCK_PATH=Path(folder) / 'test.lock'):
            errors = []
            def other():
                try:
                    with erp_sync_lock():
                        errors.append('unexpected entry')
                except ERPSyncBusy:
                    errors.append('busy')
            with erp_sync_lock(), erp_sync_lock():
                worker = threading.Thread(target=other)
                worker.start(); worker.join(timeout=3)
                self.assertFalse(worker.is_alive())
            self.assertEqual(errors, ['busy'])
            with erp_sync_lock():
                pass


class PastChangesSafetyTests(TestCase):
    def test_failed_detail_response_never_deletes_existing_transactions(self):
        from .management.commands.detect_past_changes import Command
        from unittest.mock import Mock
        part = Part.objects.create(part_no='P1', part_name='Test')
        trx = MaterialTransaction.objects.create(transaction_no='KEEP', transaction_type='TRF_ERP',
            part=part, quantity=10, erp_incoming_no='MOVE-1')
        with self.assertRaises(ValueError):
            Command()._compare_header_detail('TRF_ERP',
                Mock(return_value=(True, [dict(moveNb='MOVE')], None)),
                Mock(return_value=(False, None, 'timeout')),
                'moveNb', 'moveSq', 'moveQt', False,
                '20260901', '20260917', date(2026, 9, 1))
        self.assertTrue(MaterialTransaction.objects.filter(pk=trx.pk).exists())
