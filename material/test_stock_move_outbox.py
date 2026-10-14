from unittest.mock import patch

from django.db import transaction
from django.test import TransactionTestCase, override_settings

from orders.models import Part
from .models import MaterialTransaction, Warehouse, ERPIncomingOperation as Operation
from .erp_api import register_erp_stock_move
from .erp_outbox import dispatch, reconcile


@override_settings(ERP_ENABLED=True, ERP_OUTBOX_AUTODISPATCH=True)
class StockMoveOutboxTests(TransactionTestCase):
    def setUp(self):
        self.part = Part.objects.create(part_no='MOVE-TEST', part_name='Move test')
        self.source = Warehouse.objects.create(code='3200', name='Source')
        self.target = Warehouse.objects.create(code='3000', name='Target')
        self.trx = MaterialTransaction.objects.create(transaction_no='MOVE-TEST-1',
            transaction_type='TRANSFER', part=self.part, quantity=25,
            warehouse_from=self.source, warehouse_to=self.target)

    def send(self):
        return register_erp_stock_move(self.trx, 25, '3200', '3000')

    def test_success_and_stale_object_retry_send_once(self):
        with patch('material.erp_api.call_erp_api', return_value=(True, {'resultData': 'MV-1'}, None)) as api:
            self.assertEqual(self.send(), (True, 'MV-1', None))
            self.trx.erp_incoming_no = None
            self.trx.erp_sync_status = 'NONE'
            self.assertEqual(self.send(), (True, 'MV-1', None))
            dispatch(Operation.objects.get().pk)
            api.assert_called_once()
            self.assertEqual(api.call_args.args[0], '/apiproxy/api20A02I01101')
        self.assertEqual(Operation.objects.get().status, 'SUCCESS')
        self.trx.refresh_from_db()
        self.assertEqual(self.trx.erp_incoming_no, 'MV-1')

    def test_local_rollback_never_sends_remote_move(self):
        with patch('material.erp_api.call_erp_api') as api:
            with self.assertRaises(ValueError), transaction.atomic():
                self.send()
                api.assert_not_called()
                self.assertEqual(Operation.objects.count(), 1)
                raise ValueError('local write failed')
            api.assert_not_called()
        self.assertFalse(Operation.objects.exists())

    def test_timeout_and_exception_are_not_blindly_retried(self):
        for outcome in [(False, None, 'timeout'), RuntimeError('connection lost')]:
            with self.subTest(outcome=str(outcome)):
                self.trx = MaterialTransaction.objects.create(transaction_no='MOVE-' + str(MaterialTransaction.objects.count()),
                    transaction_type='TRANSFER', part=self.part, quantity=25,
                    warehouse_from=self.source, warehouse_to=self.target)
                with patch('material.erp_api.call_erp_api', side_effect=[outcome] if isinstance(outcome, Exception) else None,
                           return_value=outcome) as api:
                    self.send()
                    self.send()
                    job = Operation.objects.get(transaction=self.trx)
                    dispatch(job.pk)
                    api.assert_called_once()
                job.refresh_from_db()
                self.assertEqual(job.status, 'REVIEW')

    def test_pending_move_has_one_durable_request(self):
        with override_settings(ERP_OUTBOX_AUTODISPATCH=False), patch('material.erp_api.call_erp_api') as api:
            self.send()
            self.send()
            api.assert_not_called()
        self.assertEqual(Operation.objects.filter(transaction=self.trx).count(), 1)

    def test_old_success_or_uncertain_failure_is_not_replayed(self):
        self.trx.erp_incoming_no = 'OLD-MV'
        self.trx.erp_sync_status = 'SUCCESS'
        self.trx.save()
        with patch('material.erp_api.call_erp_api') as api:
            self.assertEqual(self.send(), (True, 'OLD-MV', None))
            api.assert_not_called()
        self.trx.erp_incoming_no = None
        self.trx.erp_sync_status = 'FAILED'
        self.trx.save()
        with patch('material.erp_api.call_erp_api') as api:
            self.send()
            self.send()
            api.assert_not_called()
        self.assertEqual(Operation.objects.get().status, 'REVIEW')

    def test_remote_success_with_missing_reference_requires_review(self):
        with patch('material.erp_api.call_erp_api', return_value=(True, {'resultData': ''}, None)) as api:
            self.send()
            self.send()
            api.assert_called_once()
        self.assertEqual(Operation.objects.get().status, 'REVIEW')

    def test_reconcile_move_checks_quantity_and_both_warehouses(self):
        with patch('material.erp_api.call_erp_api', return_value=(False, None, 'timeout')):
            self.send()
        job = Operation.objects.get()
        marker = f'[SCM:{job.pk.hex}]'
        detail = dict(itemCd=self.part.part_no, moveQt='25.0', fwhCd='3200', twhCd='3000', remarkDc=marker)
        with patch('material.erp_api.fetch_erp_transfer_headers', return_value=(True, [{'moveNb': 'MV-FOUND'}], None)), \
             patch('material.erp_api.fetch_erp_transfer_details', return_value=(True, [detail], None)), \
             patch('material.erp_api.call_erp_api') as api:
            self.assertTrue(reconcile(job.pk)[0])
            api.assert_not_called()
        self.trx.refresh_from_db()
        self.assertEqual(self.trx.erp_incoming_no, 'MV-FOUND')

    def test_reconcile_does_not_accept_other_destination(self):
        with patch('material.erp_api.call_erp_api', return_value=(False, None, 'timeout')):
            self.send()
        job = Operation.objects.get()
        detail = dict(itemCd=self.part.part_no, moveQt=25, fwhCd='3200', twhCd='WRONG', remarkDc=f'[SCM:{job.pk.hex}]')
        with patch('material.erp_api.fetch_erp_transfer_headers', return_value=(True, [{'moveNb': 'MV-WRONG'}], None)), \
             patch('material.erp_api.fetch_erp_transfer_details', return_value=(True, [detail], None)):
            self.assertFalse(reconcile(job.pk)[0])
        job.refresh_from_db()
        self.assertEqual(job.status, 'REVIEW')
