from contextlib import ExitStack, nullcontext
from datetime import date
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import Sum
from django.test import TestCase, override_settings
from django.urls import reverse
from django.contrib.auth.models import User
import json

from orders.models import Part
from material.models import MaterialStock, MaterialTransaction, StockSyncRun, Warehouse
from material.erp_api import sync_stock_from_erp, sync_erp_incoming, register_erp_stock_move
from material.erp_sync_pipeline import sync_inventory
from material.erp_sync_lock import ERPSyncBusy


class ERPAuthoritativeStockTests(TestCase):
    def setUp(self):
        self.part = Part.objects.create(part_no='ZR700', part_name='Resin')
        self.wh = Warehouse.objects.create(code='3000', name='Factory')

    def stock(self, qty, lot=None):
        return MaterialStock.objects.create(part=self.part, warehouse=self.wh, quantity=qty, lot_no=lot)

    def balance(self, qty):
        return {'whCd': '3000', 'itemCd': 'ZR700', 'invQt1': qty}

    def total(self):
        return MaterialStock.objects.aggregate(total=Sum('quantity'))['total'].quantize(Decimal('.0001'))

    def test_live_case_corrects_to_677_3337_despite_all_seven_ledger_errors(self):
        self.stock(4828)
        StockSyncRun.objects.create(status='failed', error='수불 오류 7건: 총량 보정을 보류했습니다.')
        with ExitStack() as stack:
            for name in ['incoming', 'issue', 'receipt', 'stock_transfer', 'adjustments', 'outgoing']:
                errors = ['DETAIL_EMPTY'] * (5 if name == 'incoming' else 2 if name == 'outgoing' else 0)
                stack.enter_context(patch(f'material.erp_api.sync_erp_{name}', return_value=(0, 0, len(errors), errors)))
            stack.enter_context(patch('material.erp_api.fetch_erp_stock', return_value=(True, [self.balance('677.3337')], None)))
            result = sync_inventory()
            self.assertEqual(result['ledger_errors'], 7)
            self.assertIsNone(result['stock']['error'])
            self.assertEqual(self.total(), Decimal('677.3337'))
            self.assertEqual(StockSyncRun.objects.first().status, 'partial')
            self.assertEqual(sync_inventory()['stock']['adjusted'], 0)
        self.assertEqual(MaterialTransaction.objects.get().quantity, Decimal('4150.6663'))

    def test_fractional_fifo_preserves_recent_lot_and_audit_then_is_idempotent(self):
        old = self.stock(1000, date(2026, 1, 1))
        recent = self.stock('500.1234', date(2026, 9, 1))
        self.stock('3327.8766')
        with patch('material.erp_api.fetch_erp_stock', return_value=(True, [self.balance('677.3337')], None)):
            self.assertIsNone(sync_stock_from_erp()['error'])
            self.assertEqual(sync_stock_from_erp()['adjusted'], 0)
        old.refresh_from_db(); recent.refresh_from_db()
        self.assertEqual(old.quantity, Decimal('177.2103'))
        self.assertEqual(recent.quantity, Decimal('500.1234'))
        self.assertEqual(self.total(), Decimal('677.3337'))
        self.assertTrue(MaterialTransaction.objects.filter(quantity=Decimal('-822.7897')).exists())

    def test_explicit_negative_erp_balance_is_applied_without_negative_assigned_lot(self):
        lot = self.stock(10, date(2026, 1, 1))
        with patch('material.erp_api.fetch_erp_stock', return_value=(True, [self.balance('-0.1234')], None)):
            self.assertIsNone(sync_stock_from_erp()['error'])
            self.assertEqual(sync_stock_from_erp()['adjusted'], 0)
        lot.refresh_from_db()
        self.assertEqual(lot.quantity, 0)
        self.assertEqual(self.total(), Decimal('-0.1234'))

    def test_precision_or_api_error_never_overwrites_current_stock(self):
        self.stock(4828)
        for value in ['677.33371', 'NaN', 'Infinity', '10000000000000000']:
            with self.subTest(value=value), patch('material.erp_api.fetch_erp_stock', return_value=(True, [self.balance(value)], None)):
                self.assertTrue(sync_stock_from_erp()['error'])
                self.assertEqual(self.total(), 4828)
        with patch('material.erp_api.fetch_erp_stock', return_value=(False, [], 'timeout')):
            with self.assertRaisesRegex(CommandError, 'timeout'):
                call_command('sync_erp_stock', stdout=StringIO())
        self.assertEqual(self.total(), 4828)

    def test_stock_only_command_does_not_import_history_and_reports_busy(self):
        self.stock(4828)
        with patch('material.erp_api.fetch_erp_stock', return_value=(True, [self.balance('677.3337')], None)), patch(
            'material.erp_api.sync_erp_incoming'
        ) as history:
            call_command('sync_erp_stock', stdout=StringIO())
            history.assert_not_called()
        self.assertEqual(self.total(), Decimal('677.3337'))
        with patch('material.erp_api.sync_stock_from_erp', side_effect=ERPSyncBusy('busy')):
            with self.assertRaisesRegex(CommandError, '보정 미실행'):
                call_command('sync_erp_stock', stdout=StringIO())

    def test_stock_only_command_waits_for_lock_and_rejects_invalid_timeout(self):
        with patch('material.management.commands.sync_erp_stock.erp_sync_lock',
                   side_effect=[ERPSyncBusy('busy'), nullcontext()]), patch(
            'material.management.commands.sync_erp_stock.time.sleep'
        ) as sleep, patch('material.erp_api.sync_stock_from_erp', return_value={'adjusted': 0, 'error': None}) as stock:
            call_command('sync_erp_stock', wait_seconds=10, stdout=StringIO())
            sleep.assert_called_once()
            stock.assert_called_once()
        with patch('material.erp_api.sync_stock_from_erp') as stock:
            with self.assertRaises(CommandError):
                call_command('sync_erp_stock', wait_seconds=-1, stdout=StringIO())
            stock.assert_not_called()

    def test_fractional_incoming_import_is_retained_and_idempotent(self):
        with patch('material.erp_api.fetch_erp_incoming_headers', return_value=(True, [
            {'rcvNb': 'RV1', 'rcvDt': '20261019', 'whCd': '3000'}], None)), patch(
            'material.erp_api.fetch_erp_incoming_detail', return_value=(True, [
                {'rcvSq': 1, 'itemCd': 'ZR700', 'rcvQt': '0.3337'}], None)):
            result = sync_erp_incoming('20261019', '20261019')
            self.assertEqual(result[2], 0, result[3])
            self.assertEqual(sync_erp_incoming('20261019', '20261019')[2], 0)
        self.assertEqual(self.total(), Decimal('0.3337'))
        self.assertEqual(MaterialTransaction.objects.get().quantity, Decimal('0.3337'))

    def test_stock_screen_and_lot_api_preserve_four_decimal_places(self):
        self.stock('677.3337')
        self.client.force_login(User.objects.create(username='stock-admin', is_superuser=True))
        with patch('material.erp_api.fetch_erp_stock', return_value=(True, [self.balance('677.3337')], None)):
            response = self.client.get(reverse('material:stock_list'), {
                'search_triggered': 'yes', 'q': 'ZR700', 'show_erp': '1'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '677.3337')
        self.assertEqual(response.context['stocks'][0]['erp_diff'], 0)
        response = self.client.get(reverse('material:wms_lot_details', args=['ZR700']))
        self.assertEqual(response.json()['total_quantity'], 677.3337)

    @override_settings(ERP_ENABLED=True)
    def test_stock_button_runs_current_stock_only(self):
        self.client.force_login(User.objects.create(username='stock-admin', is_superuser=True))
        with patch('material.erp_api.sync_stock_from_erp', return_value={'adjusted': 1, 'error': None}) as stock, patch(
            'material.erp_sync_pipeline.sync_inventory'
        ) as history:
            response = self.client.post(reverse('material:erp_stock_manage'), {'action': 'sync_stock'})
        self.assertEqual(response.status_code, 302)
        stock.assert_called_once()
        history.assert_not_called()

    @override_settings(ERP_ENABLED=True)
    def test_fractional_move_payload_is_json_serializable(self):
        trx = MaterialTransaction.objects.create(transaction_no='MOVE', transaction_type='TRANSFER',
                                                 part=self.part, quantity=Decimal('0.3337'))
        with patch('material.erp_outbox.enqueue_registration', return_value=(True, 'MV1', None)) as enqueue:
            register_erp_stock_move(trx, trx.quantity, '3000', '3200')
        payload = enqueue.call_args.args[1]
        self.assertEqual(json.loads(json.dumps(payload))['detail'][0]['moveQt'], 0.3337)
