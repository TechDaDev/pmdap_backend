import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from guardians.serializers import MinorCreateSerializer
from guardians.services import (
    approve_guardian_relationship,
    guardian_can_access_minor,
    normalize_family_number,
    revoke_guardian_relationship,
)
from tests.factories import UserFactory
from tests.test_minors_guardians import (
    create_minor,
    create_verified_guardian,
    document_model,
    national_card_payload,
    patient_model,
    relationship_model,
)

pytestmark = pytest.mark.django_db


def _pending_parent(api_client, *, relationship="FATHER", child_family="FAM-100"):
    guardian, guardian_profile, agent = create_verified_guardian(family=" FAM  -100 ")
    payload = national_card_payload(relationship=relationship)
    payload.pop("family_number")
    response = create_minor(api_client, guardian, payload=payload)
    assert response.status_code == 201
    minor = patient_model().objects.exclude(pk=guardian_profile.pk).get()
    child_card = document_model().objects.get(patient=minor)
    child_card.family_number = child_family
    child_card.save(update_fields=("family_number", "updated_at"))
    from identities.services import approve_identity_document

    approve_identity_document(document=child_card, agent=agent)
    return guardian, minor, agent, relationship_model().objects.get()


def test_family_number_normalization_is_conservative():
    assert normalize_family_number(" 1012lom10290019303 ") == "1012LOM10290019303"
    assert normalize_family_number(" OI 01 ") == "OI01"


def test_minor_create_rejects_client_family_number():
    serializer = MinorCreateSerializer(data=national_card_payload())
    assert not serializer.is_valid()
    assert "family_number" in serializer.errors


def test_parent_family_mismatch_cannot_be_approved(api_client):
    _, _, agent, relationship = _pending_parent(
        api_client, child_family="DIFFERENT-FAMILY"
    )

    with pytest.raises(Exception) as exc_info:
        approve_guardian_relationship(relationship=relationship, agent=agent)

    assert getattr(exc_info.value, "status_code", None) == 409
    relationship.refresh_from_db()
    assert relationship.family_number_result == "MISMATCH"
    assert relationship.active is False


def test_superuser_can_review_matching_parent_and_access_is_preserved(api_client):
    guardian, minor, _, relationship = _pending_parent(
        api_client, child_family="FAM-100"
    )
    superuser = UserFactory(
        email="guardian-review-superuser@example.com",
        role="ADMIN",
        status="ACTIVE",
        is_staff=True,
        is_superuser=True,
    )

    approved = approve_guardian_relationship(relationship=relationship, agent=superuser)

    assert approved.family_number_result == "MATCH"
    assert guardian_can_access_minor(guardian, minor) is True


def test_revoke_relationship_immediately_disconnects_access(api_client):
    guardian, minor, _, relationship = _pending_parent(
        api_client, relationship="MOTHER", child_family="FAM-100"
    )
    superuser = UserFactory(
        email="guardian-revoke-superuser@example.com",
        role="ADMIN",
        status="ACTIVE",
        is_staff=True,
        is_superuser=True,
    )
    approve_guardian_relationship(relationship=relationship, agent=superuser)
    assert guardian_can_access_minor(guardian, minor) is True

    revoked = revoke_guardian_relationship(
        relationship=relationship,
        actor=guardian,
        reason="Guardian requested revocation.",
    )

    assert revoked.active is False
    assert revoked.ended_at is not None
    assert revoked.ended_reason == "REVOKED"
    assert guardian_can_access_minor(guardian, minor) is False


def test_verified_card_replacement_revalidates_and_ends_parent_access(api_client):
    guardian, minor, agent, relationship = _pending_parent(
        api_client, relationship="FATHER", child_family="FAM-100"
    )
    approve_guardian_relationship(relationship=relationship, agent=agent)
    source = document_model().objects.get(patient=minor, status="CURRENT")
    from identities.services import approve_identity_document, submit_identity_document
    from tests.test_minors_guardians import image_upload

    replacement = submit_identity_document(
        patient=minor,
        actor=guardian,
        replaces=source,
        validated_data={
            "document_type": "UNIFIED_NATIONAL_CARD",
            "document_number": "CHILD-REPLACEMENT",
            "national_number": "CHILD-REPLACEMENT-NAT",
            "family_number": "NEW-FAMILY",
            "issuing_country": "IQ",
            "front_image": image_upload("child-replacement-front.png"),
            "back_image": image_upload("child-replacement-back.png"),
        },
    )

    approve_identity_document(document=replacement, agent=agent)

    relationship.refresh_from_db()
    assert relationship.family_number_result == "MISMATCH"
    assert relationship.active is False
    assert relationship.ended_reason == "RELATIONSHIP_INVALIDATED"
    assert guardian_can_access_minor(guardian, minor) is False


def test_ordinary_staff_cannot_review_but_superuser_can(api_client):
    _, _, _, relationship = _pending_parent(api_client)
    ordinary = UserFactory(
        email="ordinary-staff@example.com", role="ADMIN", status="ACTIVE", is_staff=True
    )
    superuser = UserFactory(
        email="review-superuser@example.com",
        role="ADMIN",
        status="ACTIVE",
        is_staff=True,
        is_superuser=True,
    )
    from tests.test_minors_guardians import VERIFY_RELATIONSHIPS, auth

    auth(api_client, ordinary)
    assert api_client.get(VERIFY_RELATIONSHIPS).status_code == 403
    auth(api_client, superuser)
    assert api_client.get(VERIFY_RELATIONSHIPS).status_code == 200
    assert (
        api_client.get(f"{VERIFY_RELATIONSHIPS}{relationship.uuid}/").status_code == 200
    )


def test_guardian_db_service_query_budgets(api_client):
    guardian, minor, agent, relationship = _pending_parent(api_client)
    with CaptureQueriesContext(connection) as approve_queries:
        approve_guardian_relationship(relationship=relationship, agent=agent)
    with CaptureQueriesContext(connection) as authorization_queries:
        assert guardian_can_access_minor(guardian, minor) is True
    with CaptureQueriesContext(connection) as revoke_queries:
        revoke_guardian_relationship(
            relationship=relationship, actor=guardian, reason="Synthetic benchmark."
        )
    from guardians.services import submit_guardian_relationship

    with CaptureQueriesContext(connection) as request_queries:
        submit_guardian_relationship(
            guardian=guardian, minor=minor, relationship_type="MOTHER"
        )

    assert len(request_queries) <= 16
    assert len(approve_queries) <= 30
    assert len(revoke_queries) <= 10
    assert len(authorization_queries) <= 6


def test_guardian_ops_queue_is_private_and_actions_are_post_only(api_client, client):
    _, _, agent, relationship = _pending_parent(api_client)
    agent.is_staff = True
    agent.save(update_fields=("is_staff", "updated_at"))
    client.force_login(agent)

    queue = client.get("/admin/guardian-verification/")
    review = client.get(f"/admin/guardian-verification/{relationship.uuid}/")
    approve_get = client.get(
        f"/admin/guardian-verification/{relationship.uuid}/approve/"
    )

    assert queue.status_code == review.status_code == 200
    rendered = (queue.content + review.content).decode()
    assert "Family evidence" in queue.content.decode()
    assert "Supporting name evidence" in queue.content.decode()
    assert "Official evidence files" in queue.content.decode()
    assert "Match" in queue.content.decode()
    assert "2015-05-10" not in rendered
    assert "FAM-100" not in rendered
    assert "CARD-CHILD-1" not in rendered
    assert approve_get.status_code == 405


def test_owner_revoke_api_is_post_only_and_idor_safe(api_client):
    guardian, minor, agent, relationship = _pending_parent(api_client)
    approve_guardian_relationship(relationship=relationship, agent=agent)
    from tests.test_minors_guardians import auth

    auth(api_client, guardian)
    url = f"/api/v1/minors/relationships/{relationship.uuid}/revoke/"
    assert api_client.get(url).status_code == 405
    response = api_client.post(
        url, {"reason": "Synthetic owner request."}, format="json"
    )
    assert response.status_code == 200
    assert api_client.get(f"/api/v1/minors/{minor.uuid}/").status_code == 404

    stranger, _, _ = create_verified_guardian(email="revoke-idor@example.com")
    auth(api_client, stranger)
    assert api_client.post(url, {"reason": "Denied."}, format="json").status_code == 404
