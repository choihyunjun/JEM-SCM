from io import StringIO
from unittest.mock import patch

from django.core.management import call_command, CommandError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext


class DeploymentCheckTests(TestCase):
    def test_matching_schema_passes_without_writes(self):
        output = StringIO()
        with CaptureQueriesContext(connection) as queries:
            call_command('check_deployment', stdout=output)
        self.assertIn('PASS:', output.getvalue())
        for query in queries:
            self.assertNotRegex(query['sql'].lstrip().upper(), r'^(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|REPLACE)\b')

    def test_missing_inspection_column_is_reported(self):
        original = connection.introspection.get_table_description

        def description(cursor, table):
            columns = original(cursor, table)
            if table == 'qms_importinspection':
                return [column for column in columns if column.name != 'correction_history']
            return columns

        output = StringIO()
        with patch.object(connection.introspection, 'get_table_description', side_effect=description):
            with self.assertRaises(CommandError):
                call_command('check_deployment', stdout=output)
        self.assertIn('MISSING COLUMN qms_importinspection.correction_history', output.getvalue())

    def test_missing_return_shipment_table_is_reported(self):
        original_tables = connection.introspection.table_names()
        output = StringIO()
        with patch.object(connection.introspection, 'table_names', return_value=[
            table for table in original_tables if table != 'orders_returnshipment'
        ]):
            with self.assertRaises(CommandError):
                call_command('check_deployment', stdout=output)
        self.assertIn('MISSING TABLE orders_returnshipment', output.getvalue())
