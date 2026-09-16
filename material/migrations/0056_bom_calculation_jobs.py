import uuid
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('material', '0055_wmsconfig_expiry_display_revision_and_more'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations = [
        migrations.CreateModel(name='BOMCalculationJob', fields=[
            ('id', models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
            ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
            ('updated_at', models.DateTimeField(auto_now=True)),
            ('status', models.CharField(max_length=12, default='pending')),
            ('revision', models.PositiveIntegerField(default=0)),
            ('total', models.PositiveIntegerField(default=0)),
            ('completed', models.PositiveIntegerField(default=0)),
            ('snapshot', models.JSONField(default=dict)),
            ('remaining', models.JSONField(default=dict)),
            ('summary', models.JSONField(default=dict)),
            ('error', models.CharField(max_length=300, blank=True)),
            ('owner', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to=settings.AUTH_USER_MODEL)),
        ]),
        migrations.CreateModel(name='BOMCalculationRow', fields=[
            ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
            ('sequence', models.PositiveIntegerField()),
            ('payload', models.JSONField(default=dict)),
            ('has_bom', models.BooleanField(default=False)),
            ('ready', models.BooleanField(default=False)),
            ('job', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='rows', to='material.bomcalculationjob')),
        ], options={
            'ordering': ['sequence'],
            'constraints': [models.UniqueConstraint(fields=('job', 'sequence'), name='bom_job_row_sequence')],
        }),
    ]
