from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0019_organization_and_userprofile_fields"),
        ("qms", "0012_m4review_request_content_alter_m4review_content_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="m4request",
            name="vendor_org",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="m4_requests",
                to="orders.organization",
                verbose_name="협력사(대상)",
            ),
        ),
        migrations.AddField(
            model_name="m4review",
            name="evidence_file",
            field=models.FileField(
                blank=True,
                null=True,
                upload_to="qms/m4/review/",
                verbose_name="증빙파일",
            ),
        ),
    ]
