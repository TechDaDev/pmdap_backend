import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from guardians.models import GuardianRelationshipEvent, MinorCreationRequest
from tests.test_guardian_relationships import create_approved_minor
from tests.test_minors_guardians import create_verified_guardian, relationship_model


@pytest.mark.django_db
def test_relationship_events_are_immutable(api_client):
    _, _, _, relationship = create_approved_minor(api_client)
    event = GuardianRelationshipEvent.objects.filter(relationship=relationship).first()

    event.metadata = {"changed": True}
    with pytest.raises(ValidationError):
        event.save()
    with pytest.raises(ValidationError):
        event.delete()


@pytest.mark.django_db
def test_creation_idempotency_key_is_scoped_to_guardian():
    first, _, _ = create_verified_guardian(email="first-scope@example.com")
    second, _, _ = create_verified_guardian(email="second-scope@example.com")
    MinorCreationRequest.objects.create(
        guardian_user=first, idempotency_key="same-key", request_hash="a" * 64
    )
    MinorCreationRequest.objects.create(
        guardian_user=second, idempotency_key="same-key", request_hash="b" * 64
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        MinorCreationRequest.objects.create(
            guardian_user=first, idempotency_key="same-key", request_hash="c" * 64
        )


@pytest.mark.django_db
def test_active_relationship_requires_verified_state(api_client):
    guardian, _, _ = create_verified_guardian()
    relationship = relationship_model()(
        guardian_user=guardian,
        minor_patient=guardian.patient_profile,
        relationship="MOTHER",
        verification_status="PENDING",
        active=True,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        relationship.save()
