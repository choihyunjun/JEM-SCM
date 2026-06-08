from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        ('material', '0039_moldingdailyrecord_defect_qty'),
        ('orders', '0036_link_existing_vendor_organization'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='MaterialTransferRequest',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('request_no', models.CharField(db_index=True, max_length=30, unique=True, verbose_name='요청번호')),
                ('requested_qty', models.IntegerField(verbose_name='신청수량')),
                ('status', models.CharField(choices=[('PENDING', '대기중'), ('APPROVED', '승인완료'), ('REJECTED', '반려'), ('CANCELLED', '취소')], db_index=True, default='PENDING', max_length=15, verbose_name='상태')),
                ('remark', models.TextField(blank=True, verbose_name='신청사유')),
                ('requested_at', models.DateTimeField(auto_now_add=True, verbose_name='신청일시')),
                ('approved_qty', models.IntegerField(blank=True, null=True, verbose_name='승인수량')),
                ('approved_at', models.DateTimeField(blank=True, null=True, verbose_name='승인일시')),
                ('reject_reason', models.TextField(blank=True, verbose_name='반려사유')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='등록일시')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='수정일시')),
                ('part', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='orders.part', verbose_name='품목')),
                ('requested_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='transfer_requests', to=settings.AUTH_USER_MODEL, verbose_name='신청자')),
                ('warehouse_from', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='transfer_from_requests', to='material.warehouse', verbose_name='출고창고')),
                ('warehouse_to', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='transfer_to_requests', to='material.warehouse', verbose_name='이동창고')),
                ('approved_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='approved_transfer_requests', to=settings.AUTH_USER_MODEL, verbose_name='승인자')),
                ('transfer_transaction', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='origin_requests', to='material.materialtransaction', verbose_name='이동 트랜잭션')),
            ],
            options={
                'verbose_name': '재료 이동 요청',
                'verbose_name_plural': '20. 재료 이동 요청',
                'ordering': ['-created_at'],
            },
        ),
    ]
