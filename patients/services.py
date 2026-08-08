import secrets
from datetime import date

from django.db import IntegrityError, transaction

from patients.models import PatientProfile

DIGITAL_ID_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
DIGITAL_ID_ATTEMPTS = 10


class DigitalIDGenerationError(RuntimeError):
    pass


def generate_digital_id():
    groups = [
        "".join(secrets.choice(DIGITAL_ID_ALPHABET) for _ in range(4)) for _ in range(3)
    ]
    return f"PT-{'-'.join(groups)}"


def create_patient_profile(*, user, **identity):
    if isinstance(identity.get("date_of_birth"), str):
        identity["date_of_birth"] = date.fromisoformat(identity["date_of_birth"])
    identity["nationality"] = identity["nationality"].upper()

    for _ in range(DIGITAL_ID_ATTEMPTS):
        digital_id = generate_digital_id()
        if PatientProfile.objects.filter(digital_id=digital_id).exists():
            continue
        profile = PatientProfile(user=user, digital_id=digital_id, **identity)
        profile.full_clean(validate_unique=False)
        try:
            with transaction.atomic():
                profile.save()
        except IntegrityError:
            if PatientProfile.objects.filter(digital_id=digital_id).exists():
                continue
            raise
        return profile

    raise DigitalIDGenerationError("Unable to allocate a unique Patient Digital ID.")
