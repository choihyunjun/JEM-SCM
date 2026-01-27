# Generated manually for QMS detailed permissions

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0037_change_request_v2'),
    ]

    operations = [
        # 부적합품/시정조치 권한
        migrations.AddField(
            model_name='userprofile',
            name='can_qms_nc_view',
            field=models.BooleanField(default=False, verbose_name='부적합/CAPA 조회'),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='can_qms_nc_edit',
            field=models.BooleanField(default=False, verbose_name='부적합/CAPA 등록/처리'),
        ),

        # 클레임 관리 권한
        migrations.AddField(
            model_name='userprofile',
            name='can_qms_claim_view',
            field=models.BooleanField(default=False, verbose_name='클레임 조회'),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='can_qms_claim_edit',
            field=models.BooleanField(default=False, verbose_name='클레임 등록/처리'),
        ),

        # ISIR 권한
        migrations.AddField(
            model_name='userprofile',
            name='can_qms_isir_view',
            field=models.BooleanField(default=False, verbose_name='ISIR 조회'),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='can_qms_isir_edit',
            field=models.BooleanField(default=False, verbose_name='ISIR 등록/승인'),
        ),

        # 협력사 평가 권한
        migrations.AddField(
            model_name='userprofile',
            name='can_qms_rating_view',
            field=models.BooleanField(default=False, verbose_name='협력사평가 조회'),
        ),
        migrations.AddField(
            model_name='userprofile',
            name='can_qms_rating_edit',
            field=models.BooleanField(default=False, verbose_name='협력사평가 등록'),
        ),
    ]
