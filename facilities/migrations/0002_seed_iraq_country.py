from django.db import migrations


def seed_iraq(apps, schema_editor):
    Country = apps.get_model("facilities", "Country")
    Country.objects.update_or_create(
        code="IQ",
        defaults={"name": "Iraq", "active": True},
    )


def remove_iraq(apps, schema_editor):
    Country = apps.get_model("facilities", "Country")
    Country.objects.filter(code="IQ", facilities__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("facilities", "0001_initial")]

    operations = [migrations.RunPython(seed_iraq, remove_iraq)]
