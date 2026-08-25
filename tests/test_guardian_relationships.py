import pytest
from django.utils import timezone

from tests.factories import UserFactory
from tests.test_minors_guardians import (
    MINORS,
    VERIFY_RELATIONSHIPS,
    auth,
    create_minor,
    create_verified_guardian,
    document_model,
    evidence_model,
    image_upload,
    legal_guardian_payload,
    national_card_payload,
    patient_model,
    relationship_event_model,
    relationship_model,
)


def approve_minor_document(minor, agent):
    from identities.services import approve_identity_document, submit_identity_document

    document = document_model().objects.get(patient=minor)
    if document.document_type == "UNIFIED_NATIONAL_CARD":
        document.family_number = "FAM-100"
        document.save(update_fields=("family_number", "updated_at"))
    approve_identity_document(document=document, agent=agent)
    if document.document_type != "UNIFIED_NATIONAL_CARD":
        guardian = relationship_model().objects.get(minor_patient=minor).guardian_user
        card = submit_identity_document(
            patient=minor,
            actor=guardian,
            validated_data={
                "document_type": "UNIFIED_NATIONAL_CARD",
                "document_number": f"CARD-{minor.uuid}",
                "national_number": f"NAT-{minor.uuid}",
                "family_number": "FAM-100",
                "issuing_country": "IQ",
                "front_image": image_upload("minor-card-front.png"),
                "back_image": image_upload("minor-card-back.png"),
            },
        )
        approve_identity_document(document=card, agent=agent)
    minor.refresh_from_db()
    return document


def create_approved_minor(api_client, *, payload=None):
    guardian, guardian_profile, agent = create_verified_guardian()
    create_minor(api_client, guardian, payload=payload or national_card_payload())
    minor = patient_model().objects.exclude(pk=guardian_profile.pk).get()
    approve_minor_document(minor, agent)
    relationship = relationship_model().objects.get(minor_patient=minor)
    auth(api_client, agent)
    approved = api_client.post(
        f"{VERIFY_RELATIONSHIPS}{relationship.uuid}/approve/", {}, format="json"
    )
    assert approved.status_code == 200
    relationship.refresh_from_db()
    return guardian, minor, agent, relationship


@pytest.mark.django_db
def test_agent_queue_detail_and_approval(api_client):
    guardian, guardian_profile, agent = create_verified_guardian()
    create_minor(api_client, guardian)
    minor = patient_model().objects.exclude(pk=guardian_profile.pk).get()
    document = approve_minor_document(minor, agent)
    relationship = relationship_model().objects.get()
    auth(api_client, agent)

    queue = api_client.get(f"{VERIFY_RELATIONSHIPS}?status=PENDING")
    detail = api_client.get(f"{VERIFY_RELATIONSHIPS}{relationship.uuid}/")
    approved = api_client.post(
        f"{VERIFY_RELATIONSHIPS}{relationship.uuid}/approve/", {}, format="json"
    )

    assert queue.status_code == detail.status_code == approved.status_code == 200
    assert queue.json()["data"]["results"][0]["uuid"] == str(relationship.uuid)
    assert detail.json()["data"]["minor_patient"]["uuid"] == str(minor.uuid)
    assert "email" not in str(detail.json())
    relationship.refresh_from_db()
    assert relationship.verification_status == "VERIFIED"
    assert relationship.active is True
    assert relationship.verified_by == agent
    assert relationship.verified_at is not None
    assert document.patient == minor
    assert (
        relationship_event_model()
        .objects.filter(
            relationship=relationship, event_type="GUARDIAN_RELATIONSHIP_VERIFIED"
        )
        .exists()
    )


@pytest.mark.django_db
def test_relationship_approval_requires_verified_primary_minor_document(api_client):
    guardian, _, agent = create_verified_guardian()
    create_minor(api_client, guardian)
    relationship = relationship_model().objects.get()
    auth(api_client, agent)

    response = api_client.post(
        f"{VERIFY_RELATIONSHIPS}{relationship.uuid}/approve/", {}, format="json"
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "relationship_transition_conflict"


@pytest.mark.django_db
def test_exact_role_required_for_relationship_review(api_client):
    guardian, _, _ = create_verified_guardian()
    create_minor(api_client, guardian)
    relationship = relationship_model().objects.get()

    for reviewer in (guardian, UserFactory(role="ADMIN", status="ACTIVE")):
        auth(api_client, reviewer)
        assert api_client.get(VERIFY_RELATIONSHIPS).status_code == 403
        assert (
            api_client.post(
                f"{VERIFY_RELATIONSHIPS}{relationship.uuid}/approve/", {}, format="json"
            ).status_code
            == 403
        )


@pytest.mark.django_db
def test_agent_rejects_relationship_without_deleting_minor(api_client):
    guardian, guardian_profile, agent = create_verified_guardian()
    create_minor(api_client, guardian)
    minor = patient_model().objects.exclude(pk=guardian_profile.pk).get()
    relationship = relationship_model().objects.get()
    auth(api_client, agent)

    response = api_client.post(
        f"{VERIFY_RELATIONSHIPS}{relationship.uuid}/reject/",
        {"rejection_reason": "Insufficient civil evidence."},
        format="json",
    )

    assert response.status_code == 200
    relationship.refresh_from_db()
    assert relationship.verification_status == "REJECTED"
    assert relationship.active is False
    assert relationship.rejection_reason == "Insufficient civil evidence."
    assert relationship.verified_by == agent
    assert patient_model().objects.filter(pk=minor.pk).exists()
    assert document_model().objects.filter(patient=minor).exists()

    replay = api_client.post(
        f"{VERIFY_RELATIONSHIPS}{relationship.uuid}/reject/",
        {"rejection_reason": "Insufficient civil evidence."},
        format="json",
    )
    conflict = api_client.post(
        f"{VERIFY_RELATIONSHIPS}{relationship.uuid}/reject/",
        {"rejection_reason": "Different final reason."},
        format="json",
    )
    assert replay.status_code == 200
    assert conflict.status_code == 409


@pytest.mark.django_db
def test_same_approval_is_idempotent_and_conflicting_rejection_is_409(api_client):
    guardian, guardian_profile, agent = create_verified_guardian()
    create_minor(api_client, guardian)
    minor = patient_model().objects.exclude(pk=guardian_profile.pk).get()
    approve_minor_document(minor, agent)
    relationship = relationship_model().objects.get()
    auth(api_client, agent)
    url = f"{VERIFY_RELATIONSHIPS}{relationship.uuid}"

    first = api_client.post(f"{url}/approve/", {}, format="json")
    second = api_client.post(f"{url}/approve/", {}, format="json")
    conflict = api_client.post(
        f"{url}/reject/", {"rejection_reason": "Changed mind"}, format="json"
    )

    assert first.status_code == second.status_code == 200
    assert conflict.status_code == 409


@pytest.mark.django_db
def test_approval_fails_if_minor_reaches_adulthood(api_client):
    guardian, guardian_profile, agent = create_verified_guardian()
    create_minor(api_client, guardian)
    minor = patient_model().objects.exclude(pk=guardian_profile.pk).get()
    approve_minor_document(minor, agent)
    relationship = relationship_model().objects.get()
    patient_model().objects.filter(pk=minor.pk).update(
        date_of_birth=timezone.localdate().replace(year=timezone.localdate().year - 18)
    )
    auth(api_client, agent)

    response = api_client.post(
        f"{VERIFY_RELATIONSHIPS}{relationship.uuid}/approve/", {}, format="json"
    )

    assert response.status_code == 409
    relationship.refresh_from_db()
    assert relationship.verification_status == "PENDING"
    assert relationship.active is False


@pytest.mark.django_db
def test_pending_is_listed_but_detail_requires_verified_relationship(api_client):
    guardian, guardian_profile, _ = create_verified_guardian()
    create_minor(api_client, guardian)
    minor = patient_model().objects.exclude(pk=guardian_profile.pk).get()

    listed = api_client.get(MINORS)
    detail = api_client.get(f"{MINORS}{minor.uuid}/")

    assert listed.status_code == 200
    assert (
        listed.json()["data"]["results"][0]["relationship"]["verification_status"]
        == "PENDING"
    )
    assert detail.status_code == 404


@pytest.mark.django_db
def test_verified_guardian_detail_and_live_adult_boundary(api_client):
    guardian, minor, _, relationship = create_approved_minor(api_client)
    auth(api_client, guardian)
    original_digital_id = minor.digital_id
    original_events = relationship.events.count()

    allowed = api_client.get(f"{MINORS}{minor.uuid}/")
    patient_model().objects.filter(pk=minor.pk).update(
        date_of_birth=timezone.localdate().replace(year=timezone.localdate().year - 18)
    )
    denied = api_client.get(f"{MINORS}{minor.uuid}/")

    assert allowed.status_code == 200
    assert denied.status_code == 404
    minor.refresh_from_db()
    relationship.refresh_from_db()
    assert minor.digital_id == original_digital_id
    assert relationship.verification_status == "VERIFIED"
    assert relationship.active is True
    assert relationship.events.count() == original_events


@pytest.mark.django_db
def test_unrelated_and_pending_guardians_cannot_access_by_identifiers(api_client):
    guardian, guardian_profile, _ = create_verified_guardian()
    create_minor(api_client, guardian, payload=national_card_payload())
    minor = patient_model().objects.exclude(pk=guardian_profile.pk).get()
    document = document_model().objects.get(patient=minor)
    stranger, _, _ = create_verified_guardian(email="stranger@example.com")

    for actor in (guardian, stranger):
        auth(api_client, actor)
        assert api_client.get(f"{MINORS}{minor.uuid}/").status_code == 404
        assert (
            api_client.get(f"/api/v1/identity-documents/{document.uuid}/").status_code
            == 404
        )
        assert (
            api_client.get(
                f"/api/v1/identity-documents/{document.uuid}/images/front/"
            ).status_code
            == 404
        )


@pytest.mark.django_db
def test_verified_guardian_can_view_and_replace_minor_identity(api_client):
    guardian, minor, _, _ = create_approved_minor(
        api_client, payload=national_card_payload()
    )
    source = document_model().objects.get(patient=minor)
    auth(api_client, guardian)
    replacement = national_card_payload(
        document_number="CARD-CHILD-2",
        national_number="NAT-CHILD-2",
        front_image=image_upload("replacement-front.png"),
        back_image=image_upload("replacement-back.png"),
    )
    for field in (
        "full_name",
        "date_of_birth",
        "sex",
        "nationality",
        "blood_group",
        "relationship",
    ):
        replacement.pop(field)

    detail = api_client.get(f"/api/v1/identity-documents/{source.uuid}/")
    response = api_client.post(
        f"/api/v1/identity-documents/{source.uuid}/replace/",
        replacement,
        format="multipart",
    )

    assert detail.status_code == 200
    assert response.status_code == 201
    created = document_model().objects.get(uuid=response.json()["data"]["uuid"])
    source.refresh_from_db()
    assert created.patient == minor
    assert created.replaces == source
    assert source.status == "CURRENT"
    assert source.verification_status == "VERIFIED"
    assert created.events.get().actor == guardian


@pytest.mark.django_db
def test_legal_evidence_file_is_agent_only(api_client):
    guardian, _, agent = create_verified_guardian()
    create_minor(api_client, guardian, legal_guardian_payload())
    relationship = relationship_model().objects.get()
    evidence = evidence_model().objects.get()
    url = f"{VERIFY_RELATIONSHIPS}{relationship.uuid}/evidence/{evidence.uuid}/file/"

    assert api_client.get(url).status_code == 403
    auth(api_client, agent)
    response = api_client.get(url)
    assert response.status_code == 200
    assert response["Content-Type"] == "image/png"


@pytest.mark.django_db
def test_legal_guardian_relationship_with_evidence_can_be_approved(api_client):
    guardian, guardian_profile, agent = create_verified_guardian()
    create_minor(api_client, guardian, legal_guardian_payload())
    minor = patient_model().objects.exclude(pk=guardian_profile.pk).get()
    approve_minor_document(minor, agent)
    relationship = relationship_model().objects.get()
    auth(api_client, agent)

    response = api_client.post(
        f"{VERIFY_RELATIONSHIPS}{relationship.uuid}/approve/", {}, format="json"
    )

    assert response.status_code == 200
    relationship.refresh_from_db()
    assert relationship.relationship == "LEGAL_GUARDIAN"
    assert relationship.active is True


@pytest.mark.django_db
def test_multiple_guardians_are_independent_and_private(api_client):
    from guardians.services import submit_guardian_relationship

    father, minor, agent, father_relationship = create_approved_minor(api_client)
    mother, _, _ = create_verified_guardian(email="mother@example.com")
    mother_relationship = submit_guardian_relationship(
        guardian=mother,
        minor=minor,
        relationship_type="MOTHER",
    )
    assert mother_relationship.verification_status == "PENDING"
    auth(api_client, agent)
    rejected = api_client.post(
        f"{VERIFY_RELATIONSHIPS}{mother_relationship.uuid}/reject/",
        {"rejection_reason": "Evidence insufficient"},
        format="json",
    )
    assert rejected.status_code == 200

    father_relationship.refresh_from_db()
    assert father_relationship.active is True
    auth(api_client, father)
    response = api_client.get(f"{MINORS}{minor.uuid}/")
    assert response.status_code == 200
    body = str(response.json())
    assert "mother@example.com" not in body
    assert "guardian@example.com" not in body


@pytest.mark.django_db
def test_father_and_mother_can_be_verified_independently(api_client):
    from guardians.services import submit_guardian_relationship

    father, minor, agent, father_relationship = create_approved_minor(
        api_client, payload=national_card_payload(relationship="FATHER")
    )
    mother, _, _ = create_verified_guardian(email="second-parent@example.com")
    mother_relationship = submit_guardian_relationship(
        guardian=mother, minor=minor, relationship_type="MOTHER"
    )
    auth(api_client, agent)
    response = api_client.post(
        f"{VERIFY_RELATIONSHIPS}{mother_relationship.uuid}/approve/",
        {},
        format="json",
    )

    assert response.status_code == 200
    father_relationship.refresh_from_db()
    mother_relationship.refresh_from_db()
    assert father_relationship.active is True
    assert mother_relationship.active is True
    assert father_relationship.guardian_user == father
    assert mother_relationship.guardian_user == mother


@pytest.mark.django_db
def test_multiple_active_relationship_types_do_not_duplicate_minor_list(api_client):
    guardian, minor, agent, first = create_approved_minor(api_client)
    second = relationship_model().objects.create(
        guardian_user=guardian,
        minor_patient=minor,
        relationship="FATHER",
        verification_status="VERIFIED",
        active=True,
        verified_by=agent,
        verified_at=timezone.now(),
    )
    assert first.relationship != second.relationship
    auth(api_client, guardian)

    listing = api_client.get(MINORS)
    detail = api_client.get(f"{MINORS}{minor.uuid}/")

    assert listing.status_code == detail.status_code == 200
    assert listing.json()["data"]["count"] == 1


@pytest.mark.django_db
def test_duplicate_pending_guardian_relationship_is_prevented(api_client):
    from guardians.exceptions import GuardianRelationshipConflict
    from guardians.services import submit_guardian_relationship

    guardian, guardian_profile, _ = create_verified_guardian()
    create_minor(api_client, guardian)
    minor = patient_model().objects.exclude(pk=guardian_profile.pk).get()

    with pytest.raises(GuardianRelationshipConflict):
        submit_guardian_relationship(
            guardian=guardian, minor=minor, relationship_type="MOTHER"
        )


@pytest.mark.django_db
def test_guardian_losing_verified_status_loses_access_without_mutating_minor(
    api_client,
):
    guardian, minor, _, relationship = create_approved_minor(api_client)
    original_digital_id = minor.digital_id
    original_identity_status = minor.identity_status
    guardian.patient_profile.identity_status = "REJECTED"
    guardian.patient_profile.save(update_fields=("identity_status", "updated_at"))
    auth(api_client, guardian)

    detail = api_client.get(f"{MINORS}{minor.uuid}/")

    assert detail.status_code == 403
    minor.refresh_from_db()
    relationship.refresh_from_db()
    assert minor.digital_id == original_digital_id
    assert minor.identity_status == original_identity_status
    assert relationship.active is True


@pytest.mark.django_db
def test_suspended_guardian_loses_access_without_corrupting_minor(api_client):
    guardian, minor, _, relationship = create_approved_minor(api_client)
    original_digital_id = minor.digital_id
    auth(api_client, guardian)
    guardian.status = "SUSPENDED"
    guardian.save(update_fields=("status", "updated_at"))

    response = api_client.get(f"{MINORS}{minor.uuid}/")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "account_unavailable"
    minor.refresh_from_db()
    relationship.refresh_from_db()
    assert minor.digital_id == original_digital_id
    assert relationship.active is True


@pytest.mark.django_db
def test_pending_and_unrelated_guardians_cannot_replace_minor_identity(api_client):
    guardian, guardian_profile, _ = create_verified_guardian()
    create_minor(api_client, guardian, payload=national_card_payload())
    minor = patient_model().objects.exclude(pk=guardian_profile.pk).get()
    source = document_model().objects.get(patient=minor)
    stranger, _, _ = create_verified_guardian(email="replacement-stranger@example.com")
    payload = national_card_payload(
        document_number="CARD-DENIED",
        front_image=image_upload("denied-front.png"),
        back_image=image_upload("denied-back.png"),
    )
    for field in (
        "full_name",
        "date_of_birth",
        "sex",
        "nationality",
        "blood_group",
        "relationship",
    ):
        payload.pop(field)

    for actor in (guardian, stranger):
        auth(api_client, actor)
        response = api_client.post(
            f"/api/v1/identity-documents/{source.uuid}/replace/",
            payload,
            format="multipart",
        )
        assert response.status_code == 404


@pytest.mark.django_db
def test_family_number_and_digital_id_never_authorize(api_client):
    owner, minor, _, _ = create_approved_minor(
        api_client, payload=national_card_payload()
    )
    stranger, stranger_profile, _ = create_verified_guardian(
        email="same-family@example.com", family="FAM-100"
    )
    assert stranger_profile.digital_id != minor.digital_id
    auth(api_client, stranger)
    assert api_client.get(f"{MINORS}{minor.uuid}/").status_code == 404
    assert owner != stranger


@pytest.mark.django_db
def test_minor_endpoints_require_auth_and_reject_unsupported_methods(api_client):
    assert api_client.get(MINORS).status_code == 401
    assert api_client.put(MINORS, {}, format="json").status_code == 401

    guardian, _, _ = create_verified_guardian()
    auth(api_client, guardian)
    assert api_client.put(MINORS, {}, format="json").status_code == 405
