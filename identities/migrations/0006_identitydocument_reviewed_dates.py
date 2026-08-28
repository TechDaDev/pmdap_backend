from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("identities", "0005_identitydocument_review_version_and_more")]

    operations = [
        migrations.AddField(
            model_name="identitydocument",
            name="reviewed_issue_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="identitydocument",
            name="reviewed_expiry_date",
            field=models.DateField(blank=True, null=True),
        ),
    ]
