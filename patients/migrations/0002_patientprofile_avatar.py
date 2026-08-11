import patients.storage
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("patients", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="patientprofile",
            name="avatar",
            field=models.ImageField(
                blank=True,
                null=True,
                storage=patients.storage.PrivateAvatarStorage(),
            ),
        ),
    ]
