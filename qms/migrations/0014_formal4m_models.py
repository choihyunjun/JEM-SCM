from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0019_organization_and_userprofile_fields"),
        ("qms", "0013_m4request_vendor_org_m4review_evidence_file"),
    ]

    operations = [
        migrations.CreateModel(
            name="Formal4MRequest",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("formal_no", models.CharField(max_length=40, unique=True, verbose_name="정식 4M 번호")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="생성일")),
                (
                    "pre_request",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="formal_4m",
                        to="qms.m4request",
                        verbose_name="사전 4M",
                    ),
                ),
            ],
            options={
                "verbose_name": "정식 4M",
                "verbose_name_plural": "정식 4M",
            },
        ),
        migrations.CreateModel(
            name="Formal4MDocumentItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("seq", models.PositiveIntegerField(verbose_name="순번")),
                ("name", models.CharField(max_length=100, verbose_name="제출요구서류")),
                ("is_required", models.BooleanField(default=True, verbose_name="필수")),
                (
                    "review_status",
                    models.CharField(
                        choices=[("PENDING", "미검토"), ("OK", "검토완료"), ("REJECT", "반려/보완")],
                        default="PENDING",
                        max_length=10,
                        verbose_name="검토",
                    ),
                ),
                ("remark", models.TextField(blank=True, null=True, verbose_name="비고")),
                (
                    "formal",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="doc_items",
                        to="qms.formal4mrequest",
                        verbose_name="정식 4M",
                    ),
                ),
            ],
            options={
                "verbose_name": "정식 4M 제출서류 항목",
                "verbose_name_plural": "정식 4M 제출서류 항목",
                "ordering": ["seq", "id"],
                "unique_together": {("formal", "seq")},
            },
        ),
        migrations.CreateModel(
            name="Formal4MAttachment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("file", models.FileField(upload_to="qms/formal4m/", verbose_name="첨부")),
                ("uploaded_at", models.DateTimeField(auto_now_add=True, verbose_name="업로드일")),
                (
                    "uploaded_by",
                    models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to="auth.user", verbose_name="업로더"),
                ),
                (
                    "item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="attachments",
                        to="qms.formal4mdocumentitem",
                        verbose_name="제출서류 항목",
                    ),
                ),
            ],
            options={
                "verbose_name": "정식 4M 첨부",
                "verbose_name_plural": "정식 4M 첨부",
                "ordering": ["-uploaded_at", "-id"],
            },
        ),
    ]
