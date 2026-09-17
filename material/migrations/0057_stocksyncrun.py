from django.db import migrations, models
import django.utils.timezone


class Migration(migrations.Migration):
    dependencies = [('material', '0056_bom_calculation_jobs')]
    operations = [
        migrations.CreateModel(
            name='StockSyncRun',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('started_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
                ('status', models.CharField(default='running', max_length=12)),
                ('summary', models.JSONField(default=dict)),
                ('error', models.TextField(blank=True)),
            ],
            options={'ordering': ['-id']},
        ),
    ]
