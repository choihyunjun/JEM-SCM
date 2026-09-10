from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from orders.models import Vendor, Part
from .models import MaterialStock, MaterialTransaction, Warehouse, ERPIncomingOperation as Operation
from .erp_outbox import enqueue_registration, enqueue_deletion, dispatch, reconcile, retry_after_verification


class StockIdentityTests(TestCase):
    def setUp(self):
        self.part = Part.objects.create(part_no='P', part_name='Part')
        self.wh = Warehouse.objects.create(code='2000', name='Good')

    def test_null_combinations_and_populated_identity_are_unique(self):
        for lot, batch in [(None, None), (date(2026, 1, 1), None), (None, 'A'), (date(2026, 1, 1), 'A')]:
            kwargs = dict(warehouse=self.wh, part=self.part, lot_no=lot, production_lot=batch)
            with self.subTest(lot=lot, batch=batch):
                MaterialStock.objects.create(**kwargs, quantity=10)
                with self.assertRaises(IntegrityError), transaction.atomic():
                    MaterialStock.objects.create(**kwargs, quantity=20)
                self.assertEqual(MaterialStock.objects.get(**kwargs).quantity, 10)

    def test_different_batches_remain_separate(self):
        for batch in ('A', 'B'):
            MaterialStock.objects.create(warehouse=self.wh, part=self.part,
                lot_no=date(2026, 1, 1), production_lot=batch, quantity=10)
        self.assertEqual(MaterialStock.objects.count(), 2)


@override_settings(ERP_ENABLED=True, ERP_COMPANY_CODE='TEST', ERP_OUTBOX_AUTODISPATCH=True)
class ERPOutboxTests(TransactionTestCase):
    def setUp(self):
        self.actor = User.objects.create(username='erp-tester', is_superuser=True)
        self.vendor = Vendor.objects.create(code='TEST', name='Test', erp_code='TEST')
        self.part = Part.objects.create(part_no='P', part_name='Part', vendor=self.vendor)
        self.wh = Warehouse.objects.create(code='2000', name='Good')
        self.trx = MaterialTransaction.objects.create(transaction_no='TEST-IN', transaction_type='IN_MANUAL',
            part=self.part, vendor=self.vendor, quantity=10, warehouse_to=self.wh, actor=self.actor)

    def body(self):
        return {'coCd': 'TEST', 'keyDt': '20260910', 'whCd': '2000', 'remarkDc': 'SCM',
                'detail': [{'itemCd': 'P', 'rcvQt': 10, 'remarkDc': 'SCM'}]}

    def test_no_remote_call_before_commit_and_rollback_discards_request(self):
        with patch('material.erp_api.call_erp_api') as api:
            with self.assertRaises(ValueError):
                with transaction.atomic():
                    enqueue_registration(self.trx, self.body())
                    self.assertEqual(Operation.objects.count(), 1)
                    api.assert_not_called()
                    raise ValueError('local save failed')
            api.assert_not_called()
        self.assertEqual(Operation.objects.count(), 0)

    def test_committed_registration_is_sent_once_and_duplicate_call_returns_existing(self):
        def response(*args):
            self.assertFalse(transaction.get_connection().in_atomic_block)
            return True, {'resultData': 'RV-1'}, None
        with patch('material.erp_api.call_erp_api', side_effect=response) as api:
            with transaction.atomic():
                enqueue_registration(self.trx, self.body())
                api.assert_not_called()
            job = Operation.objects.get()
            dispatch(job.pk)
            self.assertEqual(enqueue_registration(self.trx, self.body()), (True, 'RV-1', None))
            api.assert_called_once()
        self.trx.refresh_from_db()
        self.assertEqual(self.trx.erp_incoming_no, 'RV-1')
        self.assertEqual(job.status, 'SUCCESS')

    def test_timeout_is_review_and_never_automatically_resent(self):
        with patch('material.erp_api.call_erp_api', return_value=(False, None, 'timeout')) as api:
            enqueue_registration(self.trx, self.body())
            job = Operation.objects.get()
            dispatch(job.pk)
            enqueue_registration(self.trx, self.body())
            api.assert_called_once()
        self.assertEqual(job.status, 'REVIEW')

    def test_local_result_save_failure_leaves_recoverable_request(self):
        from . import erp_outbox
        original_finish = erp_outbox._finish
        def finish(job, status, *args):
            if status == 'SUCCESS':
                raise RuntimeError('local database unavailable')
            return original_finish(job, status, *args)
        with patch('material.erp_api.call_erp_api', return_value=(True, {'resultData': 'RV-1'}, None)) as api, patch('material.erp_outbox._finish', side_effect=finish), self.assertLogs('material.erp_outbox', level='ERROR'):
            enqueue_registration(self.trx, self.body())
        self.assertEqual(Operation.objects.get().status, 'REVIEW')
        api.assert_called_once()

    @override_settings(ERP_OUTBOX_AUTODISPATCH=False)
    def test_deleting_unsent_receipt_cancels_registration(self):
        enqueue_registration(self.trx, self.body())
        job = Operation.objects.get()
        self.trx.delete()
        with patch('material.erp_api.call_erp_api') as api:
            dispatch(job.pk)
            api.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, 'CANCELLED')
        self.assertIsNone(job.transaction_id)

    def test_uncertain_receipt_cannot_be_deleted(self):
        with patch('material.erp_api.call_erp_api', return_value=(False, None, 'timeout')):
            enqueue_registration(self.trx, self.body())
        with self.assertRaises(ValueError):
            self.trx.delete()
        self.assertTrue(MaterialTransaction.objects.filter(pk=self.trx.pk).exists())

    def test_cancel_rollback_never_deletes_remote_receipt(self):
        with patch('material.erp_api.call_erp_api') as api:
            with self.assertRaises(ValueError), transaction.atomic():
                enqueue_deletion('RV-1')
                raise ValueError('second item failed')
            api.assert_not_called()
        self.assertFalse(Operation.objects.exists())

    def test_delete_is_durable_after_local_receipt_is_gone(self):
        self.trx.erp_incoming_no = 'RV-1'
        self.trx.save()
        with patch('material.erp_api.call_erp_api', return_value=(False, None, 'timeout')):
            with transaction.atomic():
                enqueue_deletion('RV-1')
                self.trx.delete()
        job = Operation.objects.get()
        self.assertEqual(job.status, 'REVIEW')
        self.assertEqual(job.erp_no, 'RV-1')
        self.assertEqual(job.payload['rcvNb'], 'RV-1')
        self.assertEqual(job.transaction_no, 'TEST-IN')

    @override_settings(ERP_OUTBOX_AUTODISPATCH=False)
    def test_replacement_waits_for_delete_success(self):
        self.trx.erp_incoming_no = 'RV-OLD'
        self.trx.save()
        enqueue_deletion('RV-OLD')
        deletion = Operation.objects.get()
        self.trx.erp_incoming_no = None
        self.trx.save()
        enqueue_registration(self.trx, self.body())
        registration = Operation.objects.get(kind='REGISTER')
        self.assertEqual(registration.depends_on, deletion)
        with patch('material.erp_api.call_erp_api', return_value=(True, {'resultData': 'RV-NEW'}, None)) as api:
            dispatch(registration.pk)
            api.assert_not_called()
            dispatch(deletion.pk)
            dispatch(registration.pk)
            self.assertEqual(api.call_count, 2)

    def test_timeout_can_be_reconciled_by_marker_without_resending(self):
        with patch('material.erp_api.call_erp_api', return_value=(False, None, 'timeout')):
            enqueue_registration(self.trx, self.body())
        job = Operation.objects.get()
        rows = [{'itemCd': 'P', 'rcvQt': 10, 'remarkDc': f'[SCM:{job.pk.hex}]'}]
        with patch('material.erp_api.fetch_erp_incoming_headers', return_value=(True, [{'rcvNb': 'RV-FOUND'}], None)), patch('material.erp_api.fetch_erp_incoming_detail', return_value=(True, rows, None)), patch('material.erp_api.call_erp_api') as api:
            self.assertTrue(reconcile(job.pk)[0])
            api.assert_not_called()
        self.trx.refresh_from_db()
        self.assertEqual(self.trx.erp_incoming_no, 'RV-FOUND')

    def test_incomplete_lookup_does_not_enable_resend(self):
        with patch('material.erp_api.call_erp_api', return_value=(False, None, 'timeout')):
            enqueue_registration(self.trx, self.body())
        job = Operation.objects.get()
        with patch('material.erp_api.fetch_erp_incoming_headers', return_value=(False, None, 'offline')):
            self.assertFalse(reconcile(job.pk)[0])
        job.refresh_from_db()
        self.assertEqual(job.status, 'REVIEW')

    def test_operator_retry_requires_a_record_and_preserves_history(self):
        with patch('material.erp_api.call_erp_api', return_value=(False, None, 'timeout')):
            enqueue_registration(self.trx, self.body())
        job = Operation.objects.get()
        with self.assertRaises(ValueError):
            retry_after_verification(job.pk, self.actor, '')
        with patch('material.erp_api.call_erp_api', return_value=(True, {'resultData': 'RV-2'}, None)):
            retry_after_verification(job.pk, self.actor, 'ERP checked; no receipt exists')
        self.assertTrue(job.events.filter(action='VERIFIED_RETRY', actor=self.actor).exists())
        job.refresh_from_db()
        self.assertEqual(job.attempts, 2)
        self.assertEqual(job.status, 'SUCCESS')

    @override_settings(ERP_OUTBOX_AUTODISPATCH=False)
    def test_editing_unsent_receipt_replaces_queued_payload(self):
        import json
        from django.test import RequestFactory
        from .views import edit_manual_incoming
        MaterialStock.objects.create(warehouse=self.wh, part=self.part, quantity=10)
        enqueue_registration(self.trx, self.body())
        original = Operation.objects.get()
        request = RequestFactory().post('/', json.dumps({
            'quantity': 15, 'date': '2026-09-10', 'lot_no': '', 'remark': 'updated',
        }), content_type='application/json')
        request.user = self.actor
        with patch('material.erp_api.fetch_erp_item_price', return_value=(100, 110)), patch('material.erp_api.call_erp_api') as api:
            response = edit_manual_incoming(request, self.trx.pk)
            self.assertTrue(json.loads(response.content)['success'], response.content)
            api.assert_not_called()
        original.refresh_from_db()
        self.assertEqual(original.status, 'CANCELLED')
        pending = Operation.objects.get(status='PENDING')
        self.assertEqual(pending.payload['detail'][0]['rcvQt'], 15)
        self.assertEqual(MaterialStock.objects.get().quantity, 15)

    def test_price_reregistration_validation_failure_preserves_existing_erp_receipt(self):
        from django.test import RequestFactory
        from .views import reregister_erp_price
        import json
        self.trx.erp_incoming_no = 'RV-KEEP'
        self.trx.save()
        request = RequestFactory().post('/', '{}', content_type='application/json')
        request.user = self.actor
        with patch('material.erp_api.fetch_erp_item_price', return_value=(0, 0)), patch('material.erp_api.call_erp_api') as api, self.assertLogs('material.views', level='ERROR'):
            response = reregister_erp_price(request, self.trx.pk)
            self.assertFalse(json.loads(response.content)['success'])
            api.assert_not_called()
        self.trx.refresh_from_db()
        self.assertEqual(self.trx.erp_incoming_no, 'RV-KEEP')
        self.assertFalse(Operation.objects.exists())

    def test_existing_successful_receipt_does_not_require_another_price_lookup(self):
        from .erp_api import register_erp_incoming
        self.trx.erp_incoming_no = 'RV-KEEP'
        self.trx.erp_sync_status = 'SUCCESS'
        self.trx.save()
        with patch('material.erp_api.fetch_erp_item_price') as price:
            self.assertEqual(register_erp_incoming(self.trx, 10, '2000'), (True, 'RV-KEEP', None))
            price.assert_not_called()
