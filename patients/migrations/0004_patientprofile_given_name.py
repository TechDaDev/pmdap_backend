from django.db import migrations, models


def backfill_confirmed_given_names(apps, schema_editor):
    PatientProfile = apps.get_model("patients", "PatientProfile")
    for profile in PatientProfile.objects.exclude(father_name="").iterator():
        full_name = " ".join((profile.full_name or "").split())
        suffix = " ".join(
            part
            for part in (
                " ".join((profile.father_name or "").split()),
                " ".join((profile.grandfather_name or "").split()),
            )
            if part
        )
        marker = f" {suffix}"
        if suffix and full_name.endswith(marker):
            given_name = full_name[: -len(marker)].strip()
            if given_name:
                PatientProfile.objects.filter(pk=profile.pk).update(
                    given_name=given_name
                )


class Migration(migrations.Migration):
    dependencies = [("patients", "0003_patientprofile_father_name_and_more")]

    operations = [
        migrations.AddField(
            model_name="patientprofile",
            name="given_name",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.RunPython(backfill_confirmed_given_names, migrations.RunPython.noop),
    ]
