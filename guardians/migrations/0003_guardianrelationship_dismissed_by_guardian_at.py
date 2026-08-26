"""Add patient-facing guardian relationship dismissal.

M29.3: rejected/revoked relationship rows may be dismissed from the guardian's
active My Children list. `dismissed_by_guardian_at` is purely presentational —
the immutable relationship row, its events, and audit history are preserved and
the REJECTED/REVOKED status is never rewritten. A dismissed relationship can be
re-considered later by re-dismissing the dismissal (setting the field to null)
or simply by submitting a fresh request.
"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        (
            "guardians",
            "0002_remove_guardianrelationship_guardian_one_active_relationship_type_and_more",
        ),
    ]

    operations = [
        migrations.AddField(
            model_name="guardianrelationship",
            name="dismissed_by_guardian_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="guardianrelationshipevent",
            name="event_type",
            field=models.CharField(
                choices=[
                    ("MINOR_CREATED", "Minor created"),
                    (
                        "GUARDIAN_RELATIONSHIP_SUBMITTED",
                        "Relationship submitted",
                    ),
                    (
                        "MINOR_IDENTITY_DOCUMENT_SUBMITTED",
                        "Minor identity document submitted",
                    ),
                    ("FAMILY_NUMBER_MATCHED", "Family number matched"),
                    ("FAMILY_NUMBER_MISMATCHED", "Family number mismatched"),
                    ("GUARDIAN_RELATIONSHIP_VERIFIED", "Relationship verified"),
                    ("GUARDIAN_RELATIONSHIP_REJECTED", "Relationship rejected"),
                    ("GUARDIAN_RELATIONSHIP_ENDED", "Relationship ended"),
                    (
                        "GUARDIAN_RELATIONSHIP_DISMISSED",
                        "Relationship dismissed by guardian",
                    ),
                ],
                max_length=48,
            ),
        ),
    ]
