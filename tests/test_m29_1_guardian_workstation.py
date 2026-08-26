import pytest
from django.urls import reverse

from guardians.services import can_approve_guardian_relationship
from identities.services import approve_identity_document
from tests.factories import UserFactory
from tests.test_minors_guardians import (
    create_minor,
    create_verified_guardian,
    document_model,
    evidence_model,
    legal_guardian_payload,
    national_card_payload,
    patient_model,
    relationship_model,
)

pytestmark = pytest.mark.django_db


def pending_father(api_client):
    guardian, guardian_profile, agent = create_verified_guardian()
    payload = national_card_payload(
        relationship="FATHER",
        full_name="Noor Layla Kareem",
        father_name="Layla Hassan",
        grandfather_name="Kareem Ali",
    )
    response = create_minor(api_client, guardian, payload=payload)
    assert response.status_code == 201
    minor = patient_model().objects.exclude(pk=guardian_profile.pk).get()
    child_card = document_model().objects.get(patient=minor)
    child_card.family_number = "FAM-100"
    child_card.save(update_fields=("family_number", "updated_at"))
    return guardian, minor, child_card, agent, relationship_model().objects.get()


def login_reviewer(client, agent):
    agent.is_staff = True
    agent.save(update_fields=("is_staff", "updated_at"))
    client.force_login(agent)


def test_owner_repro_disables_approve_and_post_is_blocked(api_client, client):
    _, _, _, agent, relationship = pending_father(api_client)
    login_reviewer(client, agent)
    review_url = reverse("admin:ops_guardian_review", args=[relationship.pk])

    page = client.get(review_url)
    post = client.post(reverse("admin:ops_guardian_approve", args=[relationship.pk]))

    assert page.status_code == 200
    assert b"Minor identity must be verified before approval" in page.content
    assert b"Approve relationship" in page.content
    assert b"disabled" in page.content
    assert post.status_code == 409
    relationship.refresh_from_db()
    assert relationship.verification_status == "PENDING"
    assert relationship.active is False


def test_safe_unavailable_reasons_and_identity_deep_link(api_client, client):
    _, _, child_card, agent, relationship = pending_father(api_client)
    login_reviewer(client, agent)

    page = client.get(reverse("admin:ops_guardian_review", args=[relationship.pk]))
    rendered = page.content.decode()

    assert "Unavailable — minor has no verified current National Card" in rendered
    assert "Unavailable — identity is not verified" in rendered
    assert reverse("admin:ops_verification_review", args=[child_card.pk]) in rendered
    assert "FAM-100" not in rendered
    assert child_card.document_number not in rendered


def test_identity_approval_recomputes_and_enables_without_new_request(
    api_client, client
):
    _, _, child_card, agent, relationship = pending_father(api_client)
    login_reviewer(client, agent)
    before_uuid = relationship.pk

    approve_identity_document(document=child_card, agent=agent)
    relationship.refresh_from_db()
    decision = can_approve_guardian_relationship(relationship)
    page = client.get(reverse("admin:ops_guardian_review", args=[relationship.pk]))

    assert relationship.pk == before_uuid
    assert relationship.family_number_result == "MATCH"
    assert relationship.name_evidence_result == "MATCH"
    assert decision.eligible is True
    assert b"Ready for approval" in page.content
    assert b"Approve relationship" in page.content


def test_queue_readiness_and_privacy(api_client, client):
    guardian, _, child_card, agent, relationship = pending_father(api_client)
    login_reviewer(client, agent)
    page = client.get(reverse("admin:ops_guardian_queue"))
    rendered = page.content.decode()

    assert "Evidence incomplete" in rendered
    assert guardian.patient_profile.full_name in rendered
    assert relationship.minor_patient.full_name in rendered
    assert "FAM-100" not in rendered
    assert child_card.document_number not in rendered
    assert str(relationship.minor_patient.date_of_birth) not in rendered


def test_guardian_workstation_permissions(api_client, client):
    _, _, _, agent, relationship = pending_father(api_client)
    url = reverse("admin:ops_guardian_review", args=[relationship.pk])

    ordinary = UserFactory(is_staff=True, role="ADMIN", status="ACTIVE")
    client.force_login(ordinary)
    assert client.get(url).status_code == 403

    patient = UserFactory(role="PATIENT", status="ACTIVE")
    client.force_login(patient)
    assert client.get(url).status_code == 302

    superuser = UserFactory(
        is_staff=True, is_superuser=True, role="ADMIN", status="ACTIVE"
    )
    client.force_login(superuser)
    assert client.get(url).status_code == 200

    login_reviewer(client, agent)
    assert client.get(url).status_code == 200


def test_private_official_evidence_is_scoped_and_reviewer_only(api_client, client):
    guardian, _, agent = create_verified_guardian()
    response = create_minor(api_client, guardian, payload=legal_guardian_payload())
    assert response.status_code == 201
    relationship = relationship_model().objects.get()
    evidence = evidence_model().objects.get()
    url = reverse(
        "admin:ops_guardian_evidence_file", args=[relationship.pk, evidence.pk]
    )

    ordinary = UserFactory(is_staff=True, role="ADMIN", status="ACTIVE")
    client.force_login(ordinary)
    assert client.get(url).status_code == 403

    login_reviewer(client, agent)
    streamed = client.get(url)
    assert streamed.status_code == 200
    assert "no-store" in streamed["Cache-Control"]

    other_relationship = relationship_model().objects.create(
        guardian_user=guardian,
        minor_patient=relationship.minor_patient,
        relationship="MOTHER",
    )
    crossed = reverse(
        "admin:ops_guardian_evidence_file",
        args=[other_relationship.pk, evidence.pk],
    )
    assert client.get(crossed).status_code == 404
