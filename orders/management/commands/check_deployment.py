"""Read-only deployment diagnosis: migration state and model/table agreement."""
from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import connections
from django.db.migrations.executor import MigrationExecutor


class Command(BaseCommand):
    help = '미적용 마이그레이션 및 누락된 DB 테이블/컬럼 검사 (데이터 변경 없음)'

    def add_arguments(self, parser):
        parser.add_argument('--database', default='default', choices=tuple(connections))

    def handle(self, *args, **options):
        connection = connections[options['database']]
        self.stdout.write(f"DB alias: {connection.alias} / engine: {connection.vendor}")
        # Deliberately omit credentials, DB paths and business records from output.
        executor = MigrationExecutor(connection)
        executor.loader.check_consistent_history(connection)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
        for migration, backwards in plan:
            direction = 'backward' if backwards else 'forward'
            self.stdout.write(f'PENDING {migration.app_label}.{migration.name} ({direction})')

        missing_tables, missing_columns = [], []
        inspected = set()
        with connection.cursor() as cursor:
            tables = set(connection.introspection.table_names(cursor))
            for model in apps.get_models(include_auto_created=True):
                meta = model._meta
                if not meta.managed or meta.proxy or meta.swapped or meta.db_table in inspected:
                    continue
                if not meta.can_migrate(connection):
                    continue
                inspected.add(meta.db_table)
                if meta.db_table not in tables:
                    missing_tables.append(meta.db_table)
                    continue
                columns = {column.name for column in connection.introspection.get_table_description(cursor, meta.db_table)}
                for field in meta.local_concrete_fields:
                    if field.column not in columns:
                        missing_columns.append(f'{meta.db_table}.{field.column}')

        for table in sorted(missing_tables):
            self.stdout.write(f'MISSING TABLE {table}')
        for column in sorted(missing_columns):
            self.stdout.write(f'MISSING COLUMN {column}')
        self.stdout.write(
            f'Checked {len(inspected)} tables; pending migrations: {len(plan)}; '
            f'missing tables: {len(missing_tables)}; missing columns: {len(missing_columns)}'
        )
        if plan or missing_tables or missing_columns:
            raise CommandError('코드와 DB 구조가 일치하지 않습니다. 위 항목과 migrate 실행 결과를 확인하세요. DB는 변경하지 않았습니다.')
        self.stdout.write(self.style.SUCCESS('PASS: migration/table/column checks. This does not test constraints, data types or every business operation.'))
