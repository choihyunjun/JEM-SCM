from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('material', '0059_repair_purchaseorderrequest_note')]

    operations = [
        migrations.AlterField(
            model_name='erpincomingoperation', name='kind',
            field=models.CharField(max_length=10, choices=[
                ('REGISTER', '입고 등록'), ('DELETE', '입고 삭제'), ('MOVE', '재고이동 등록')]),
        ),
    ]
