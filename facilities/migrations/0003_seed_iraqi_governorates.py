from django.db import migrations


IRAQI_GOVERNORATES = (
    "Al Anbar",
    "Al-Qadisiyyah",
    "Babil",
    "Baghdad",
    "Basra",
    "Dhi Qar",
    "Diyala",
    "Duhok",
    "Erbil",
    "Halabja",
    "Karbala",
    "Kirkuk",
    "Maysan",
    "Muthanna",
    "Najaf",
    "Nineveh",
    "Saladin",
    "Sulaymaniyah",
    "Wasit",
)


def seed_governorates(apps, schema_editor):
    Country = apps.get_model("facilities", "Country")
    AdministrativeRegion = apps.get_model("facilities", "AdministrativeRegion")
    country = Country.objects.get(code="IQ")
    for name in IRAQI_GOVERNORATES:
        AdministrativeRegion.objects.update_or_create(
            country=country,
            normalized_name=name.casefold(),
            defaults={"name": name, "active": True},
        )


def remove_governorates(apps, schema_editor):
    AdministrativeRegion = apps.get_model("facilities", "AdministrativeRegion")
    AdministrativeRegion.objects.filter(
        country_id="IQ",
        normalized_name__in=[name.casefold() for name in IRAQI_GOVERNORATES],
        cities__isnull=True,
        facilities__isnull=True,
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("facilities", "0002_seed_iraq_country")]

    operations = [migrations.RunPython(seed_governorates, remove_governorates)]
