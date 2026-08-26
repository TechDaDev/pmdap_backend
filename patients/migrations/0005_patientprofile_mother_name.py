"""Add authoritative confirmed mother_name to patient profiles.

M29.3: the Iraqi National Card carries the mother's name in its front name
section. This field stores the human-confirmed/verified maternal given name
used for MOTHER guardian-relationship evidence. It is never derived from the
father's name.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("patients", "0004_patientprofile_given_name"),
    ]

    operations = [
        migrations.AddField(
            model_name="patientprofile",
            name="mother_name",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
