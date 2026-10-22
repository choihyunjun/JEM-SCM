"""Regressions found after the Decimal(20,4) stock change (SQLite REAL arithmetic,
int()/float mixing, SCM-only warehouses, status reporting, past-change detection)."""
import json
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connection
from django.db.models import F, Sum
from django.template import Context, Template
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from orders.models import Part
from .erp_api import compare_erp_stock, sync_stock_from_erp
from .erp_sync_pipeline import sync_inventory
from .models import MaterialStock, MaterialTransaction, RawMaterialLabel, StockSyncRun, Warehouse
from .quantities import erp_quantity, input_quantity
from .stock_safety import deduct_stock, quantity_change


def raw_quantity(pk):
    with connection.cursor() as cursor:
        cursor.execute('SELECT quantity FROM material_materialstock WHERE id = %s', [pk])
        return cursor.fetchone()[0]


class StockArithmeticTests(TestCase):
    def setUp(self):
        cache.clear()
        self.part = Part.objects.create(part_no='ZR700', part_name='Resin')
        self.wh = Warehouse.objects.create(code='3000', name='Factory')

    def stock(self, qty, lot=date(2026, 1, 1), wh=None):
        return MaterialStock.objects.create(part=self.part, warehouse=wh or self.wh, quantity=qty, lot_no=lot)

    def sync(self, qty, extra=()):
        rows = [{'whCd': '3000', 'itemCd': 'ZR700', 'invQt1': qty}, *extra]
        with patch('material.erp_api.fetch_erp_stock', return_value=(True, rows, None)):
            return sync_stock_from_erp()

    def test_fractional_debits_can_empty_a_lot_exactly(self):
        lot = self.stock('0.3')
        deduct_stock(lot, Decimal('0.1'))
        lot.refresh_from_db()
        deduct_stock(lot, lot.quantity)  # 0.2: failed with 0.19999999999999998 stored
        self.assertEqual(raw_quantity(lot.pk), 0)
        self.assertFalse(MaterialStock.objects.filter(quantity__gt=0).exists())

    def test_zr700_correction_then_zero_leaves_no_residue_and_compare_works(self):
        self.stock(4828)
        self.assertIsNone(self.sync('677.3337')['error'])
        self.assertEqual(raw_quantity(MaterialStock.objects.get().pk), 677.3337)
        self.assertEqual(self.sync('677.3337')['adjusted'], 0)
        self.assertIsNone(self.sync('0')['error'])
        self.assertFalse(MaterialStock.objects.filter(quantity__gt=0).exists())
        with patch('material.erp_api.fetch_erp_stock', return_value=(True, [], None)):
            ok, rows, summary, err = compare_erp_stock()
        self.assertTrue(ok, err)

    def test_repeated_fractional_consumption_never_blocks_the_correction(self):
        self.stock(1000)
        target = Decimal(1000)
        for _ in range(60):
            target -= Decimal('0.1')
            self.assertIsNone(self.sync(str(target))['error'])
        self.assertIsNone(self.sync('0')['error'])
        self.assertEqual(MaterialStock.objects.aggregate(t=Sum('quantity'))['t'], 0)

    def test_migration_rounds_existing_real_residue(self):
        lot = self.stock('0.3')
        MaterialStock.objects.filter(pk=lot.pk).update(quantity=F('quantity') - Decimal('0.1'))
        self.assertNotEqual(raw_quantity(lot.pk), 0.2)
        from importlib import import_module
        import_module('material.migrations.0062_round_stock_quantities').round_quantities(apps, None)
        self.assertEqual(raw_quantity(lot.pk), 0.2)

    def test_quantity_change_accepts_float_and_int_deltas(self):
        lot = self.stock('2.5')
        MaterialStock.objects.filter(pk=lot.pk).update(quantity=quantity_change(0.1))
        MaterialStock.objects.filter(pk=lot.pk).update(quantity=quantity_change(-1))
        lot.refresh_from_db()
        self.assertEqual(lot.quantity, Decimal('1.6'))
        self.assertEqual(raw_quantity(lot.pk), 1.6)

    def test_scm_only_warehouses_are_never_overwritten_or_reported_missing(self):
        rejected = Warehouse.objects.create(code='8200', name='Rejected')
        hold = Warehouse.objects.create(code='8100', name='Inspection hold')
        bad = self.stock(7, wh=rejected)
        waiting = self.stock(5, wh=hold)
        self.stock(10)
        result = self.sync('10', extra=[{'whCd': '8200', 'itemCd': 'ZR700', 'invQt1': '0'}])
        self.assertIsNone(result['error'])
        self.assertEqual(result['status'], 'success')
        bad.refresh_from_db(); waiting.refresh_from_db()
        self.assertEqual((bad.quantity, waiting.quantity), (7, 5))

    def test_locked_key_is_reported_and_other_keys_still_apply(self):
        other = Part.objects.create(part_no='ZR701', part_name='Resin 2')
        self.stock(10)
        MaterialStock.objects.create(part=other, warehouse=self.wh, quantity=10)
        from . import stock_reconciliation
        original = stock_reconciliation._mirror_quantity_only

        def locked_first(warehouse, part, erp_qty, expected):
            if part.part_no == 'ZR700':
                raise OperationalError('database is locked')
            return original(warehouse, part, erp_qty, expected)
        with patch.object(stock_reconciliation, '_mirror_quantity_only', side_effect=locked_first):
            result = self.sync('3', extra=[{'whCd': '3000', 'itemCd': 'ZR701', 'invQt1': '4'}])
        self.assertEqual(result['status'], 'partial')
        self.assertIn('database is locked', result['error'])
        self.assertEqual(MaterialStock.objects.get(part=other).quantity, 4)

    def test_unregistered_master_is_listed_not_a_failure(self):
        self.stock(10)
        rows = [{'whCd': '3000', 'itemCd': 'ZR700', 'invQt1': '6'},
                {'whCd': '3000', 'itemCd': 'UNREGISTERED', 'invQt1': '1'}]
        with ExitStackPatches(rows) as stack:
            result = sync_inventory()
        self.assertEqual(MaterialStock.objects.aggregate(t=Sum('quantity'))['t'], 6)
        latest = StockSyncRun.objects.first()
        self.assertEqual(latest.status, 'success')
        self.assertEqual(latest.summary['no_master'], ['3000/UNREGISTERED'])
        self.assertFalse(result['errors'])


class ExitStackPatches:
    """Ledger imports succeed with nothing new; the ERP stock response is fixed."""
    def __init__(self, stock_rows):
        from contextlib import ExitStack
        self.stack = ExitStack()
        self.rows = stock_rows

    def __enter__(self):
        for name in ['incoming', 'issue', 'receipt', 'stock_transfer', 'adjustments', 'outgoing']:
            self.stack.enter_context(patch(f'material.erp_api.sync_erp_{name}', return_value=(0, 0, 0, [])))
        self.stack.enter_context(patch('material.erp_api.fetch_erp_stock', return_value=(True, self.rows, None)))
        return self.stack

    def __exit__(self, *exc):
        return self.stack.__exit__(*exc)


class InputAndPayloadTests(TestCase):
    def test_erp_payload_keeps_integers_and_fractions(self):
        self.assertEqual(json.dumps(erp_quantity(Decimal('25.0000'))), '25')
        self.assertEqual(json.dumps(erp_quantity('2.5')), '2.5')

    def test_input_quantity_rejects_instead_of_truncating(self):
        self.assertEqual(input_quantity('2.5'), Decimal('2.5'))
        self.assertEqual(input_quantity('1,234'), Decimal('1234'))
        for bad in ['', 'abc', '0', '-1', '1.23456']:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                input_quantity(bad)

    def test_whole_stock_quantities_render_without_decimal_places(self):
        rendered = Template('{% load humanize %}{{ q|floatformat:"-4" }}|{{ q|floatformat:"-4"|intcomma }}|'
                            '{{ f|floatformat:"-4"|intcomma }}').render(
            Context({'q': Decimal('4828.0000'), 'f': Decimal('677.3337')}))
        self.assertEqual(rendered, '4828|4,828|677.3337')
        self.assertEqual(int('4828'), 4828)  # forms post the rendered value back through int()


class LabelScanTests(TestCase):
    def test_label_equal_to_lot_stock_moves_without_type_error(self):
        from .views import api_process_tag_scan
        part = Part.objects.create(part_no='ZR700', part_name='Resin')
        src = Warehouse.objects.create(code='3200', name='Raw')
        dst = Warehouse.objects.create(code='3000', name='Factory')
        lot = date(2026, 9, 1)
        MaterialStock.objects.create(part=part, warehouse=src, lot_no=lot, quantity=25)
        RawMaterialLabel.objects.create(label_id='RM-TEST-1', part=part, part_no=part.part_no,
                                        part_name=part.part_name, lot_no=lot, quantity=25)
        request = RequestFactory().post('/', json.dumps({'tag_id': 'RM-TEST-1', 'target_warehouse_code': '3000'}),
                                        content_type='application/json')
        request.user = User.objects.create(username='scan', is_superuser=True)
        response = api_process_tag_scan(request)
        self.assertTrue(json.loads(response.content)['success'], response.content)
        self.assertEqual(MaterialStock.objects.get(warehouse=src).quantity, 0)
        self.assertEqual(MaterialStock.objects.get(warehouse=dst).quantity, 25)


@override_settings(ERP_ENABLED=True)
class PastChangeDetectionTests(TestCase):
    def test_empty_document_is_reported_but_stock_is_still_corrected(self):
        part = Part.objects.create(part_no='ZR700', part_name='Resin')
        wh = Warehouse.objects.create(code='3000', name='Factory')
        MaterialStock.objects.create(part=part, warehouse=wh, quantity=4828)
        kept = MaterialTransaction.objects.create(
            transaction_no='KEEP', transaction_type='OUT_ERP', part=part, quantity=-5,
            erp_incoming_no='IS2609000372-1', date=timezone.now())
        today = timezone.localdate().strftime('%Y%m%d')
        stock_rows = [{'whCd': '3000', 'itemCd': 'ZR700', 'invQt1': '677.3337'}]
        patches = [
            patch('material.erp_api.fetch_erp_incoming_headers', return_value=(True, [], None)),
            patch('material.erp_api.fetch_erp_issue_headers', return_value=(True, [], None)),
            patch('material.erp_api.fetch_erp_transfer_headers', return_value=(True, [], None)),
            patch('material.erp_api.fetch_erp_outgoing_headers',
                  return_value=(True, [{'isuNb': 'IS2609000372', 'isuDt': today}], None)),
            patch('material.erp_api.fetch_erp_outgoing_details', return_value=(True, [], None)),
            patch('material.erp_api.fetch_erp_receipt_list', return_value=(True, [], None)),
            patch('material.erp_api.sync_erp_adjustments', return_value=(0, 0, 0, [])),
            patch('material.erp_api.fetch_erp_stock', return_value=(True, stock_rows, None)),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        with self.assertRaises(CommandError) as raised:
            call_command('detect_past_changes', '--days', '30', stdout=StringIO(), stderr=StringIO())
        self.assertIn('DETAIL_EMPTY', str(raised.exception))
        self.assertEqual(MaterialStock.objects.aggregate(t=Sum('quantity'))['t'], Decimal('677.3337'))
        self.assertTrue(MaterialTransaction.objects.filter(pk=kept.pk).exists())
        self.assertEqual(StockSyncRun.objects.first().status, 'partial')
