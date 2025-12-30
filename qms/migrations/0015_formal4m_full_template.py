from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("qms", "0014_formal4m_models"),
    ]

    operations = [
        migrations.AddField(
            model_name="formal4mrequest",
            name="template_type",
            field=models.CharField(choices=[("BASIC", "기본"), ("FULL", "확장")], default="BASIC", max_length=10, verbose_name="양식 유형"),
        ),
        migrations.CreateModel(
            name="Formal4MApproval",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("is_approved", models.BooleanField(default=False, verbose_name="사내승인")),
                ("approval_no", models.CharField(blank=True, max_length=40, null=True, verbose_name="승인번호")),
                ("judgment_date", models.DateField(blank=True, null=True, verbose_name="판정일자")),
                ("remark", models.CharField(blank=True, max_length=200, null=True, verbose_name="비고")),
                ("formal_request", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="approval", to="qms.formal4mrequest", verbose_name="정식 4M")),
            ],
            options={
                "verbose_name": "정식4M 사내승인",
                "verbose_name_plural": "정식4M 사내승인",
            },
        ),
        migrations.CreateModel(
            name="Formal4MInspectionResult",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("inspection_item", models.CharField(max_length=100, verbose_name="검사항목")),
                ("spec", models.CharField(blank=True, max_length=100, null=True, verbose_name="규격")),
                ("method", models.CharField(blank=True, max_length=100, null=True, verbose_name="검사방법")),
                ("judgment", models.CharField(blank=True, max_length=30, null=True, verbose_name="판정")),
                ("remark", models.CharField(blank=True, max_length=200, null=True, verbose_name="비고")),
                ("attachment", models.FileField(blank=True, null=True, upload_to="formal4m/inspection/", verbose_name="첨부")),
                ("formal_request", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="inspection_results", to="qms.formal4mrequest", verbose_name="정식 4M")),
            ],
            options={
                "verbose_name": "정식4M 검토결과",
                "verbose_name_plural": "정식4M 검토결과",
            },
        ),
        migrations.CreateModel(
            name="Formal4MScheduleItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("oem", models.CharField(blank=True, max_length=50, null=True, verbose_name="OEM")),
                ("item_name", models.CharField(max_length=100, verbose_name="항목")),
                ("is_required", models.BooleanField(default=False, verbose_name="진행유무(필수)")),
                ("plan_date", models.DateField(blank=True, null=True, verbose_name="계획일")),
                ("owner_name", models.CharField(blank=True, max_length=50, null=True, verbose_name="담당자")),
                ("department", models.CharField(blank=True, max_length=50, null=True, verbose_name="부서")),
                ("note", models.CharField(blank=True, max_length=200, null=True, verbose_name="비고")),
                ("formal_request", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="schedule_items", to="qms.formal4mrequest", verbose_name="정식 4M")),
            ],
            options={
                "verbose_name": "정식4M 일정항목",
                "verbose_name_plural": "정식4M 일정항목",
            },
        ),
        migrations.CreateModel(
            name="Formal4MStageRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("stage", models.CharField(choices=[("ISIR", "ISIR제출"), ("OEM_APPROVAL", "OEM승인"), ("INTERNAL_APPLY", "사내적용"), ("CUSTOMER_APPLY", "고객적용"), ("OTHER", "기타")], max_length=20, verbose_name="단계")),
                ("record_date", models.DateField(blank=True, null=True, verbose_name="일자")),
                ("remark", models.CharField(blank=True, max_length=200, null=True, verbose_name="비고")),
                ("attachment", models.FileField(blank=True, null=True, upload_to="formal4m/stages/", verbose_name="문서첨부")),
                ("formal_request", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="stage_records", to="qms.formal4mrequest", verbose_name="정식 4M")),
            ],
            options={
                "verbose_name": "정식4M 단계기록",
                "verbose_name_plural": "정식4M 단계기록",
            },
        ),
    ]
