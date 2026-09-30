from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('material', '0057_stocksyncrun')]

    operations = [
        migrations.AddField(
            model_name='wmsconfig',
            name='hide_scan_history',
            field=models.BooleanField(default=False, verbose_name='기간별 투입이력 숨기기'),
        ),
        migrations.AddField(
            model_name='wmsconfig',
            name='scan_history_hide_from',
            field=models.DateField(blank=True, null=True, verbose_name='투입이력 숨김 시작일'),
        ),
        migrations.AddField(
            model_name='wmsconfig',
            name='scan_history_hide_to',
            field=models.DateField(blank=True, null=True, verbose_name='투입이력 숨김 종료일'),
        ),
    ]
