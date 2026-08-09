from django.core.exceptions import ValidationError
from django.db import transaction

from facilities.exceptions import InvalidLocationHierarchy
from facilities.models import HealthcareFacility


def create_healthcare_facility(**values):
    try:
        with transaction.atomic():
            return HealthcareFacility.objects.create(**values)
    except ValidationError as exc:
        raise InvalidLocationHierarchy() from exc


def update_healthcare_facility(*, facility, **values):
    with transaction.atomic():
        locked = HealthcareFacility.objects.select_for_update().get(pk=facility.pk)
        for field, value in values.items():
            setattr(locked, field, value)
        try:
            locked.save()
        except ValidationError as exc:
            raise InvalidLocationHierarchy() from exc
        return locked


def deactivate_healthcare_facility(*, facility):
    return update_healthcare_facility(facility=facility, active=False)
