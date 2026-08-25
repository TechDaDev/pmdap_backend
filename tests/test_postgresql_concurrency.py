import threading

import pytest
from django.db import close_old_connections, connection

from guardians.models import GuardianRelationshipEvent
from tests.factories import UserFactory
from tests.test_minors_guardians import (
    birth_document_payload,
    create_verified_guardian,
    document_model,
    image_upload,
    national_card_payload,
    patient_model,
    relationship_model,
)

pytestmark = [pytest.mark.postgresql, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def private_identity_storage(settings, tmp_path):
    settings.IDENTITY_FILE_ROOT = tmp_path / "private-identity"
    settings.IDENTITY_FILE_MAX_BYTES = 1024 * 1024


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL-only concurrency test")


def run_concurrently(*operations):
    barrier = threading.Barrier(len(operations))
    results = []
    failures = []

    def run(operation):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            results.append(operation())
        except Exception as exc:  # result is asserted by each concurrency scenario
            failures.append(exc)
        finally:
            close_old_connections()

    threads = [
        threading.Thread(target=run, args=(operation,)) for operation in operations
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()
    return results, failures


def test_concurrent_national_card_replacement_approvals_keep_one_current():
    require_postgresql()
    from identities.models import IdentityDocument
    from identities.services import approve_identity_document, submit_identity_document

    guardian, profile, first_agent = create_verified_guardian()
    source = document_model().objects.get(patient=profile)
    replacement = submit_identity_document(
        patient=profile,
        actor=guardian,
        replaces=source,
        validated_data={
            "document_type": "UNIFIED_NATIONAL_CARD",
            "document_number": "REPLACEMENT-A",
            "national_number": "NAT-A",
            "family_number": "FAM-100",
            "issuing_country": "IQ",
            "front_image": image_upload("replacement-a-front.png"),
            "back_image": image_upload("replacement-a-back.png"),
        },
    )
    competing = IdentityDocument.objects.create(
        patient=profile,
        document_type=source.document_type,
        document_number="REPLACEMENT-B",
        national_number="NAT-B",
        family_number="FAM-100",
        issuing_country="IQ",
        front_image=source.front_image,
        back_image=source.back_image,
        replaces=source,
    )
    second_agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")
    results, failures = run_concurrently(
        lambda: approve_identity_document(document=replacement, agent=first_agent).uuid,
        lambda: approve_identity_document(document=competing, agent=second_agent).uuid,
    )

    assert len(results) == 1
    assert len(failures) == 1
    assert (
        IdentityDocument.objects.filter(
            patient=profile,
            document_type="UNIFIED_NATIONAL_CARD",
            status="CURRENT",
            verification_status="VERIFIED",
        ).count()
        == 1
    )


def test_concurrent_duplicate_guardian_approvals_are_consistent():
    require_postgresql()
    from guardians.serializers import MinorCreateSerializer
    from guardians.services import approve_guardian_relationship, create_minor
    from identities.services import approve_identity_document

    guardian, guardian_profile, agent = create_verified_guardian()
    payload = national_card_payload()
    payload.pop("family_number")
    serializer = MinorCreateSerializer(data=payload)
    assert serializer.is_valid(), serializer.errors
    created = create_minor(
        guardian=guardian,
        idempotency_key="approval-race",
        validated_data=serializer.validated_data,
    )
    minor = patient_model().objects.exclude(pk=guardian_profile.pk).get()
    minor_card = document_model().objects.get(patient=minor)
    minor_card.family_number = "FAM-100"
    minor_card.save(update_fields=("family_number", "updated_at"))
    approve_identity_document(document=minor_card, agent=agent)
    relationship = created.relationship
    results, failures = run_concurrently(
        lambda: (
            approve_guardian_relationship(relationship=relationship, agent=agent).uuid
        ),
        lambda: (
            approve_guardian_relationship(relationship=relationship, agent=agent).uuid
        ),
    )

    assert len(results) == 2
    assert not failures
    relationship.refresh_from_db()
    assert relationship.active is True
    assert relationship.verification_status == "VERIFIED"
    assert (
        GuardianRelationshipEvent.objects.filter(
            relationship=relationship, event_type="GUARDIAN_RELATIONSHIP_VERIFIED"
        ).count()
        == 1
    )


def test_concurrent_idempotent_minor_creation_produces_one_minor():
    require_postgresql()
    from guardians.serializers import MinorCreateSerializer
    from guardians.services import create_minor

    guardian, guardian_profile, _ = create_verified_guardian()

    def operation():
        serializer = MinorCreateSerializer(data=birth_document_payload())
        assert serializer.is_valid(), serializer.errors
        result = create_minor(
            guardian=guardian,
            idempotency_key="minor-race",
            validated_data=serializer.validated_data,
        )
        return result.created

    results, failures = run_concurrently(operation, operation)

    assert not failures
    assert sorted(results) == [False, True]
    assert patient_model().objects.exclude(pk=guardian_profile.pk).count() == 1
    assert relationship_model().objects.count() == 1


def test_concurrent_duplicate_pending_relationships_keep_one_live_tuple():
    require_postgresql()
    guardian, _, _ = create_verified_guardian()
    patient = patient_model().objects.create(
        digital_id="39999999999999999",
        full_name="Synthetic Minor",
        date_of_birth="2015-01-01",
        sex="UNSPECIFIED",
        nationality="IQ",
    )

    def operation():
        return (
            relationship_model()
            .objects.create(
                guardian_user=guardian,
                minor_patient=patient,
                relationship="FATHER",
            )
            .uuid
        )

    results, failures = run_concurrently(operation, operation)

    assert len(results) == 1
    assert len(failures) == 1
    assert (
        relationship_model()
        .objects.filter(
            guardian_user=guardian,
            minor_patient=patient,
            relationship="FATHER",
            verification_status="PENDING",
            ended_at__isnull=True,
        )
        .count()
        == 1
    )
