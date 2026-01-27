
import csv
from django.core.management.base import BaseCommand
from orders.models import Vendor


class Command(BaseCommand):
    help = 'Import vendors from ERP CSV file'

    def add_arguments(self, parser):
        parser.add_argument('csv_file', type=str, help='Path to CSV file')
        parser.add_argument('--dry-run', action='store_true', help='Preview without saving')

    def handle(self, *args, **options):
        csv_file = options['csv_file']
        dry_run = options['dry_run']
        
        created = 0
        updated = 0
        skipped = 0
        errors = []

        with open(csv_file, 'r', encoding='utf-8-sig') as f:
            reader = csv.reader(f)
            rows = list(reader)
        
        # Skip header rows (first 5 rows)
        i = 5
        while i < len(rows) - 1:
            row1 = rows[i]
            row2 = rows[i + 1] if i + 1 < len(rows) else [''] * 9
            
            # Skip if row1 doesn't have code (empty or header row)
            if len(row1) < 2 or not row1[0] or not row1[0].strip().isdigit():
                i += 1
                continue
            
            try:
                code = row1[0].strip()
                name = row1[1].strip() if len(row1) > 1 else ''
                # row1[2] = 구분 (skip)
                biz_reg_no = row1[3].strip() if len(row1) > 3 else ''
                biz_type = row1[4].strip() if len(row1) > 4 else ''
                # row1[5] = 우편번호, row1[6] = 메일, row1[7] = 금융기관, row1[8] = 예금주
                
                address = row2[0].strip() if len(row2) > 0 else ''
                representative = row2[3].strip() if len(row2) > 3 else ''
                biz_item = row2[4].strip() if len(row2) > 4 else ''
                
                if not name:
                    i += 2
                    continue
                
                if dry_run:
                    self.stdout.write(f"[DRY] {code}: {name}")
                else:
                    vendor, was_created = Vendor.objects.update_or_create(
                        code=code,
                        defaults={
                            'name': name,
                            'erp_code': code,
                            'biz_registration_number': biz_reg_no or None,
                            'representative': representative or None,
                            'address': address or None,
                            'biz_type': biz_type or None,
                            'biz_item': biz_item or None,
                        }
                    )
                    if was_created:
                        created += 1
                    else:
                        updated += 1
                        
            except Exception as e:
                errors.append(f"Row {i}: {e}")
                skipped += 1
            
            i += 2  # Move to next vendor (2 rows per vendor)
        
        self.stdout.write(self.style.SUCCESS(
            f"완료: 신규 {created}개, 업데이트 {updated}개, 스킵 {skipped}개"
        ))
        if errors:
            for err in errors[:10]:
                self.stdout.write(self.style.ERROR(err))
