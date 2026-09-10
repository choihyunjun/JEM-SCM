import json
from datetime import date
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import RequestFactory, TestCase

from orders.models import Part
from .erp_api import sync_erp_receipt
from .models import (
    MaterialStock, MaterialTransaction, MaterialTransferRequest,
    MaterialTransferRequestLine, ProductionLotItem, Warehouse,
)
from .views import api_transfer_request_lots, transfer_request_approve, transfer_request_revoke


class TransferRequestLotTests(TestCase):
    def setUp(self):
        self.user = User.objects.create(username='lot-test', is_superuser=True)
        self.part = Part.objects.create(part_no='LOT-P', part_name='LOT part')
        self.source = Warehouse.objects.create(code='2000', name='Source')
        self.target = Warehouse.objects.create(code='3000', name='Target')
        self.day = date(2026, 9, 1)
        self.a = self.stock('2633511', 10)
        self.b = self.stock('2634511', 20)
        self.req = MaterialTransferRequest.objects.create(request_no='LOT-REQ', requested_by=self.user)
        self.line = MaterialTransferRequestLine.objects.create(
            request=self.req, part=self.part, requested_qty=20,
        )
        erp_patch = patch('material.erp_api.register_erp_stock_move')
        self.erp = erp_patch.start()
        self.addCleanup(erp_patch.stop)

    def stock(self, batch, qty, **kwargs):
        return MaterialStock.objects.create(
            warehouse=kwargs.get('warehouse', self.source),
            part=kwargs.get('part', self.part), lot_no=kwargs.get('lot_no', self.day),
            production_lot=batch, quantity=qty,
        )

    def request(self, data):
        request = RequestFactory().post('/audit/', data)
        request.user = self.user
        request.session = {}
        request._messages = FallbackStorage(request)
        return request

    def approve(self, selection, qty=20, extra=None):
        data = {
            'warehouse_from': self.source.pk, 'warehouse_to': self.target.pk,
            f'process_{self.line.pk}': 'on', f'lot_{self.line.pk}': selection,
            f'qty_{self.line.pk}': qty,
        }
        data.update(extra or {})
        return transfer_request_approve(self.request(data), self.req.pk)

    def assert_unchanged(self):
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.req.refresh_from_db()
        self.assertEqual((self.a.quantity, self.b.quantity), (10, 20))
        self.assertEqual(self.req.status, 'PENDING')
        self.assertFalse(MaterialStock.objects.filter(warehouse=self.target).exists())
        self.assertFalse(MaterialTransaction.objects.exists())
        self.erp.assert_not_called()

    def test_api_distinguishes_same_date_batches_and_template_submits_stock_id(self):
        request = RequestFactory().get('/', {'part_no': self.part.part_no, 'warehouse_id': self.source.pk})
        request.user = self.user
        lots = json.loads(api_transfer_request_lots(request).content)['lots']
        self.assertEqual(lots[0]['lot_no'], '__FIFO__')
        self.assertEqual([lot['stock_id'] for lot in lots[1:]], [self.a.pk, self.b.pk])
        self.assertEqual(lots[1]['lot_no'], lots[2]['lot_no'])
        request = RequestFactory().get('/')
        request.user = self.user
        page = transfer_request_approve(request, self.req.pk)
        self.assertContains(page, '`stock:${lot.stock_id}`')

    def test_selected_batch_moves_and_revoke_restores_same_batch(self):
        self.assertEqual(self.approve(f'stock:{self.b.pk}').status_code, 302)
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.assertEqual((self.a.quantity, self.b.quantity), (10, 0))
        trx = MaterialTransaction.objects.get()
        self.assertEqual((trx.production_lot, trx.lot_no, trx.quantity), ('2634511', self.day, 20))
        target = MaterialStock.objects.get(warehouse=self.target)
        self.assertEqual((target.production_lot, target.quantity), ('2634511', 20))
        self.erp.assert_called_once()
        with patch('material.erp_api.delete_erp_stock_move'):
            transfer_request_revoke(self.request({}), self.req.pk)
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        target.refresh_from_db()
        self.assertEqual((self.a.quantity, self.b.quantity, target.quantity), (10, 20, 0))

    def test_selected_batch_shortage_cannot_borrow_other_batch(self):
        self.approve(f'stock:{self.a.pk}', 20)
        self.assert_unchanged()

    def test_old_date_or_missing_selection_requires_reload(self):
        for value in ('2026-09-01', '', 'stock:invalid', 'stock:-1'):
            with self.subTest(value=value):
                self.approve(value)
                self.assert_unchanged()

    def test_stock_must_belong_to_selected_part_and_source(self):
        other_part = Part.objects.create(part_no='OTHER', part_name='Other')
        foreign = self.stock('2634511', 50, part=other_part)
        other_wh = Warehouse.objects.create(code='4000', name='Other')
        elsewhere = self.stock('2634511', 50, warehouse=other_wh)
        for stock in (foreign, elsewhere):
            self.approve(f'stock:{stock.pk}')
            self.assert_unchanged()

    def test_date_only_and_null_stock_remain_selectable_by_id(self):
        for lot in (self.day, None):
            with self.subTest(lot=lot):
                stock = self.stock(None, 5, lot_no=lot)
                self.approve(f'stock:{stock.pk}', 5)
                trx = MaterialTransaction.objects.get()
                self.assertEqual((trx.lot_no, trx.production_lot, trx.quantity), (lot, None, 5))
                with patch('material.erp_api.delete_erp_stock_move'):
                    transfer_request_revoke(self.request({}), self.req.pk)

    def test_fifo_still_splits_by_production_batch(self):
        self.approve('__FIFO__', 25)
        self.a.refresh_from_db()
        self.b.refresh_from_db()
        self.assertEqual((self.a.quantity, self.b.quantity), (0, 5))
        self.assertEqual(dict(MaterialStock.objects.filter(warehouse=self.target).values_list(
            'production_lot', 'quantity')), {'2633511': 10, '2634511': 15})
        self.assertEqual(MaterialTransaction.objects.count(), 2)

    def test_multiple_lines_cannot_overdraw_same_stock_and_roll_back_together(self):
        line2 = MaterialTransferRequestLine.objects.create(request=self.req, part=self.part, requested_qty=15)
        for selection in (f'stock:{self.b.pk}', '__FIFO__'):
            qty = 15 if selection.startswith('stock:') else 20
            with self.subTest(selection=selection), self.assertLogs('material.views', level='ERROR'):
                self.approve(selection, qty, {
                    f'process_{line2.pk}': 'on', f'lot_{line2.pk}': selection, f'qty_{line2.pk}': qty,
                })
            self.assert_unchanged()
            self.line.refresh_from_db()
            self.assertIsNone(self.line.approved_qty)


class ProductionReceiptAtomicTests(TestCase):
    def setUp(self):
        self.part = Part.objects.create(part_no='LOT-P', part_name='LOT part')
        ProductionLotItem.objects.create(part=self.part)
        self.wh = Warehouse.objects.create(code='2000', name='Receipt')
        self.row = {
            'rcvNb': 'RCV-1', 'itemCd': self.part.part_no, 'rcvQt': 10,
            'rcvDt': '20260901', 'twhCd': self.wh.code, 'lotNb': '2633511',
        }

    def sync(self, rows=None):
        with patch('material.erp_api.fetch_erp_receipt_list', return_value=(True, rows or [self.row], None)):
            return sync_erp_receipt('20260901', '20260910')

    def test_history_failure_rolls_back_batch_and_mirror_then_retry_counts_once(self):
        mirror = MaterialStock.objects.create(warehouse=self.wh, part=self.part, quantity=100)
        with patch('material.erp_api._create_trx', side_effect=RuntimeError('history unavailable')), \
                self.assertLogs('material.erp_api', level='ERROR'):
            self.assertEqual(self.sync()[:3], (0, 0, 1))
        mirror.refresh_from_db()
        self.assertEqual(mirror.quantity, 100)
        self.assertEqual(MaterialStock.objects.count(), 1)
        self.assertFalse(MaterialTransaction.objects.exists())
        self.assertEqual(self.sync()[:3], (1, 0, 0))
        self.assertEqual(self.sync()[:3], (0, 1, 0))
        mirror.refresh_from_db()
        self.assertEqual(mirror.quantity, 90)
        self.assertEqual(MaterialStock.objects.get(production_lot='2633511').quantity, 10)
        self.assertEqual(MaterialTransaction.objects.count(), 1)

    def test_existing_batch_increment_rolls_back_on_history_failure(self):
        stock = MaterialStock.objects.create(warehouse=self.wh, part=self.part,
            lot_no=date(2026, 9, 1), production_lot='2633511', quantity=7)
        with patch('material.erp_api._create_trx', side_effect=RuntimeError('failed')), \
                self.assertLogs('material.erp_api', level='ERROR'):
            self.sync()
        stock.refresh_from_db()
        self.assertEqual(stock.quantity, 7)
        self.sync()
        stock.refresh_from_db()
        self.assertEqual(stock.quantity, 17)

    def test_same_date_batches_stay_separate_and_duplicate_receipt_is_skipped(self):
        second = dict(self.row, rcvNb='RCV-2', lotNb='2634511', rcvQt=20)
        self.assertEqual(self.sync([self.row, self.row, second])[:3], (2, 1, 0))
        self.assertEqual(dict(MaterialStock.objects.values_list('production_lot', 'quantity')),
                         {'2633511': 10, '2634511': 20})

    def test_invalid_receipt_date_does_not_reuse_previous_rows_date(self):
        invalid = dict(self.row, rcvNb='RCV-BAD', rcvDt='invalid')
        with self.assertLogs('material.erp_api', level='ERROR'):
            self.assertEqual(self.sync([self.row, invalid])[:3], (1, 0, 1))
        self.assertEqual(MaterialStock.objects.get().quantity, 10)
        self.assertEqual(MaterialTransaction.objects.count(), 1)
