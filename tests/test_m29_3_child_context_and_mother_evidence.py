"""M29.3 — child-context + guardian-request dismissal + mother evidence.

Synthetic data only. Covers:
- rejected/revoked relationship dismissal (presentation-only, audit preserved)
- mother-name card extraction + authoritative persistence
- MOTHER evidence MATCH / MISMATCH / UNAVAILABLE + approval matrix
- father + legal-guardian regressions
- minor document upload targets the minor (never the guardian)
"""
import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image

from audit.models import AuditLog
from guardians.exceptions import GuardianRelationshipConflict
from guardians.models import GuardianRelationship, GuardianRelationshipEvent
from guardians.services import (
    EVIDENCE_POLICY_VERSION,
    approve_guardian_relationship,
    can_approve_guardian_relationship,
)
from identities import extraction, mrz
from identities.services import approve_identity_document
from tests.test_m29_1_guardian_workstation import pending_parent
from tests.test_minors_guardians import (
    create_minor,
    create_verified_guardian,
    document_model,
    image_upload,
    national_card_payload,
    patient_model,
    relationship_event_model,
    relationship_model,
)

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------- #
# A. Rejected request dismissal
# --------------------------------------------------------------------------- #

def _rejected_relationship(api_client, guardian):
    response = create_minor(
        api_client,
        guardian,
        national_card_payload(document_number="DISMISS-CARD-1"),
        key="m29-3-dismiss-create",
    )
    assert response.status_code == 201
    relationship = relationship_model().objects.latest("created_at")
    relationship.verification_status = GuardianRelationship.VerificationStatus.REJECTED
    relationship.active = False
    relationship.verified_at = relationship.created_at
    relationship.rejection_reason = "Synthetic review rejection"
    relationship.save(
        update_fields=(
            "verification_status",
            "active",
            "verified_at",
            "rejection_reason",
            "updated_at",
        )
    )
    return relationship


def _dismiss_url(relationship):
    return f"/api/v1/guardian-relationships/{relationship.uuid}/dismiss/"


def test_guardian_dismisses_rejected_request_and_status_stays_rejected(
    api_client,
):
    guardian, _, _ = create_verified_guardian()
    relationship = _rejected_relationship(api_client, guardian)
    event_count_before = relationship_event_model().objects.filter(
        relationship=relationship
    ).count()

    response = api_client.post(_dismiss_url(relationship), {}, format="json")

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["dismissed_at"] is not None
    assert body["can_dismiss"] is False
    relationship.refresh_from_db()
    # Presentation only — REJECTED status is never rewritten.
    assert relationship.verification_status == "REJECTED"
    assert relationship.dismissed_by_guardian_at is not None
    assert (
        relationship_event_model()
        .objects.filter(relationship=relationship)
        .count()
        == event_count_before + 1
    )
    assert relationship.events.filter(
        event_type=GuardianRelationshipEvent.EventType.DISMISSED
    ).exists()


def test_dismiss_is_idempotent(api_client):
    guardian, _, _ = create_verified_guardian()
    relationship = _rejected_relationship(api_client, guardian)

    first = api_client.post(_dismiss_url(relationship), {}, format="json")
    second = api_client.post(_dismiss_url(relationship), {}, format="json")

    assert first.status_code == 200
    assert second.status_code == 200
    # Only ONE dismissal event is ever written.
    assert (
        relationship_event_model()
        .objects.filter(
            relationship=relationship,
            event_type=GuardianRelationshipEvent.EventType.DISMISSED,
        )
        .count()
        == 1
    )


def test_cannot_dismiss_active_verified_relationship(api_client):
    guardian, minor, child_card, agent, relationship = pending_parent(api_client)
    approve_identity_document(document=child_card, agent=agent)
    relationship.refresh_from_db()
    approved = approve_guardian_relationship(relationship=relationship, agent=agent)
    assert approved.active is True

    response = api_client.post(_dismiss_url(approved), {}, format="json")

    assert response.status_code == 409
    approved.refresh_from_db()
    assert approved.dismissed_by_guardian_at is None


def test_cannot_dismiss_pending_relationship(api_client):
    guardian, _, _ = create_verified_guardian()
    relationship = _rejected_relationship(api_client, guardian)
    relationship.verification_status = GuardianRelationship.VerificationStatus.PENDING
    relationship.save(update_fields=("verification_status", "updated_at"))

    response = api_client.post(_dismiss_url(relationship), {}, format="json")

    assert response.status_code == 409
    relationship.refresh_from_db()
    assert relationship.dismissed_by_guardian_at is None


def test_other_guardian_cannot_dismiss(api_client):
    guardian, _, _ = create_verified_guardian()
    relationship = _rejected_relationship(api_client, guardian)
    other, _, _ = create_verified_guardian(email="other-guardian@example.com")
    api_client.force_authenticate(user=other)

    response = api_client.post(_dismiss_url(relationship), {}, format="json")

    assert response.status_code == 404
    relationship.refresh_from_db()
    assert relationship.dismissed_by_guardian_at is None


def test_dismiss_records_audit_and_preserves_history(api_client):
    guardian, _, _ = create_verified_guardian()
    relationship = _rejected_relationship(api_client, guardian)
    audit_before = AuditLog.objects.filter(resource_uuid=relationship.uuid).count()
    original_events = list(
        relationship_event_model()
        .objects.filter(relationship=relationship)
        .order_by("created_at", "uuid")
    )

    api_client.post(_dismiss_url(relationship), {}, format="json")

    assert (
        AuditLog.objects.filter(resource_uuid=relationship.uuid).count()
        == audit_before + 1
    )
    assert AuditLog.objects.filter(
        resource_uuid=relationship.uuid,
        action=AuditLog.Action.GUARDIAN_RELATIONSHIP_DISMISSED,
    ).exists()
    # The original immutable events are untouched; only one new event appended.
    remaining_events = list(
        relationship_event_model()
        .objects.filter(relationship=relationship)
        .order_by("created_at", "uuid")
    )
    assert remaining_events[:-1] == original_events


def test_list_default_hides_dismissed_include_history_shows(api_client):
    guardian, _, _ = create_verified_guardian()
    relationship = _rejected_relationship(api_client, guardian)
    dismiss = api_client.post(_dismiss_url(relationship), {}, format="json")
    assert dismiss.status_code == 200

    hidden = api_client.get("/api/v1/guardian-relationships/")
    shown = api_client.get("/api/v1/guardian-relationships/?include_history=true")

    assert hidden.status_code == 200
    uuids = [row["uuid"] for row in hidden.json()["data"]["results"]]
    assert str(relationship.uuid) not in uuids
    shown_uuids = [row["uuid"] for row in shown.json()["data"]["results"]]
    assert str(relationship.uuid) in shown_uuids


def test_dismiss_revoked_relationship_is_allowed(api_client):
    guardian, minor, child_card, agent, relationship = pending_parent(api_client)
    approve_identity_document(document=child_card, agent=agent)
    relationship.refresh_from_db()
    approved = approve_guardian_relationship(relationship=relationship, agent=agent)
    from guardians.services import revoke_guardian_relationship

    revoked = revoke_guardian_relationship(
        relationship=approved, actor=guardian, reason="Synthetic revocation"
    )
    assert revoked.ended_at is not None

    response = api_client.post(_dismiss_url(revoked), {}, format="json")

    assert response.status_code == 200
    revoked.refresh_from_db()
    assert revoked.dismissed_by_guardian_at is not None
    assert revoked.ended_reason == "REVOKED"


# --------------------------------------------------------------------------- #
# C. Mother-name extraction + authoritative persistence
# --------------------------------------------------------------------------- #

def _cd(value):
    return str(mrz.check_digit(value))


def _mother_card_lines():
    lines = [
        extraction.SideLine("FRONT", "الاسم اناو TESTNAME", 0.9),
        extraction.SideLine("FRONT", "اباوك TESTFATHER", 0.9),
        extraction.SideLine("FRONT", "ابابيرTESTGRAND", 0.9),
        extraction.SideLine("FRONT", "ادايك TESTMOTHER", 0.9),
        extraction.SideLine("FRONT", "الجنس اركمز ذكر", 0.9),
        extraction.SideLine("FRONT", "123456789012", 0.9),
        extraction.SideLine("FRONT", "H12345678", 0.9),
    ]
    for line in [
        "ID" + "IRQ" + "H12345678" + _cd("H12345678") + "900101202601",
        "900517" + _cd("900517") + "M" + "360101" + _cd("360101") + "IRQ",
        "TESTGRANDFATHER<<TESTNAME",
    ]:
        lines.append(extraction.SideLine("ROI_MRZ", line.ljust(30, "<"), 0.9))
    return lines


def test_mother_name_parser_field_is_deterministic():
    fields, _, _ = extraction.extract_identity(
        "UNIFIED_NATIONAL_CARD", _mother_card_lines()
    )
    assert fields["mother_name"]["value"] == "TESTMOTHER"
    assert fields["mother_name"]["source"] == "FRONT_PRINTED"
    # Father/given fields are still distinct and untouched.
    assert fields["name"]["value"] == "TESTNAME"
    assert fields["father_name"]["value"] == "TESTFATHER"


def test_mother_name_not_invented_when_card_has_no_maternal_field():
    lines = [
        extraction.SideLine("FRONT", "الاسم اناو TESTNAME", 0.9),
        extraction.SideLine("FRONT", "اباوك TESTFATHER", 0.9),
        extraction.SideLine("FRONT", "الجنس اركمز ذكر", 0.9),
    ]
    fields, _, _ = extraction.extract_identity("UNIFIED_NATIONAL_CARD", lines)
    assert "mother_name" not in fields


def test_minor_create_persists_authoritative_mother_name(api_client):
    guardian, _, _ = create_verified_guardian()
    payload = national_card_payload(
        document_number="MOTHER-CARD-2",
        mother_name="SyntheticMother",
    )
    response = create_minor(
        api_client,
        guardian,
        payload,
        key="m29-3-mother-persist",
    )

    assert response.status_code == 201, response.data
    minor = patient_model().objects.exclude(
        pk=guardian.patient_profile.pk
    ).get()
    assert minor.mother_name == "SyntheticMother"


# --------------------------------------------------------------------------- #
# C4–C7. MOTHER evidence
# --------------------------------------------------------------------------- #

def _evaluated_mother(
    api_client,
    *,
    adult_given="A",
    minor_mother="A",
):
    guardian, minor, child_card, agent, relationship = pending_parent(
        api_client, relationship_type="MOTHER"
    )
    guardian.patient_profile.given_name = adult_given
    guardian.patient_profile.save(update_fields=("given_name", "updated_at"))
    minor.mother_name = minor_mother
    minor.save(update_fields=("mother_name", "updated_at"))
    approve_identity_document(document=child_card, agent=agent)
    relationship.refresh_from_db()
    return guardian, minor, agent, relationship


@pytest.mark.parametrize(
    ("adult_given", "minor_mother", "expected"),
    (
        ("A", "A", "MATCH"),
        ("  فاطمة  ", "فاطمة", "MATCH"),
        ("ﻋﺒﺪ أﺣﻤﺪ", "عبد احمد", "MATCH"),
        ("فاطمة", "فاطمه", "MISMATCH"),
        ("A", "B", "MISMATCH"),
        ("A", "", "UNAVAILABLE"),
        ("", "A", "UNAVAILABLE"),
    ),
)
def test_mother_evidence_matrix(api_client, adult_given, minor_mother, expected):
    _, _, _, relationship = _evaluated_mother(
        api_client, adult_given=adult_given, minor_mother=minor_mother
    )

    decision = can_approve_guardian_relationship(relationship)

    assert decision.name_evidence_kind == "MOTHER"
    assert decision.name_result == expected
    assert decision.eligible is (expected == "MATCH")


def test_mother_family_match_but_mother_mismatch_is_denied(api_client):
    _, _, agent, relationship = _evaluated_mother(
        api_client, adult_given="A", minor_mother="Different"
    )

    decision = can_approve_guardian_relationship(relationship)

    assert decision.family_result == "MATCH"
    assert decision.name_result == "MISMATCH"
    assert decision.eligible is False
    assert decision.code == "NOT_ELIGIBLE_MOTHER_NAME_EVIDENCE"
    with pytest.raises(GuardianRelationshipConflict):
        approve_guardian_relationship(relationship=relationship, agent=agent)


def test_mother_family_match_mother_unavailable_is_denied(api_client):
    _, _, agent, relationship = _evaluated_mother(
        api_client, adult_given="A", minor_mother=""
    )

    decision = can_approve_guardian_relationship(relationship)

    assert decision.family_result == "MATCH"
    assert decision.name_result == "UNAVAILABLE"
    assert (
        decision.name_explanation
        == "Verified maternal-name evidence is unavailable."
    )
    assert decision.eligible is False
    with pytest.raises(GuardianRelationshipConflict):
        approve_guardian_relationship(relationship=relationship, agent=agent)


def test_mother_match_plus_family_match_is_approvable(api_client):
    guardian, minor, agent, relationship = _evaluated_mother(
        api_client, adult_given="SyntheticMother", minor_mother="SyntheticMother"
    )
    # Re-evaluate now that the child card is verified.
    child_card = document_model().objects.get(
        patient=minor,
        verification_status="VERIFIED",
        status="CURRENT",
    )
    approve_identity_document(document=child_card, agent=agent)
    relationship.refresh_from_db()

    decision = can_approve_guardian_relationship(relationship)

    assert decision.family_result == "MATCH"
    assert decision.name_result == "MATCH"
    assert decision.eligible is True
    approved = approve_guardian_relationship(relationship=relationship, agent=agent)
    assert approved.active is True


def test_father_evidence_still_uses_father_name(api_client):
    guardian, minor, child_card, agent, relationship = pending_parent(api_client)
    guardian.patient_profile.given_name = "A"
    guardian.patient_profile.save(update_fields=("given_name", "updated_at"))
    minor.father_name = "A"
    minor.mother_name = "TotallyDifferentMother"
    minor.save(update_fields=("father_name", "mother_name", "updated_at"))
    approve_identity_document(document=child_card, agent=agent)
    relationship.refresh_from_db()

    decision = can_approve_guardian_relationship(relationship)

    assert decision.name_evidence_kind == "FATHER"
    assert decision.name_result == "MATCH"
    # Father evidence must never read the mother name.
    assert decision.eligible is True


def test_legal_guardian_regression_uses_official_evidence(api_client):
    guardian, _, _ = create_verified_guardian()
    payload = national_card_payload(
        relationship="LEGAL_GUARDIAN",
        document_number="LG-CARD-1",
        evidence_type="COURT_DOCUMENT",
        evidence_file=image_upload("court-order.png"),
    )
    response = create_minor(
        api_client,
        guardian,
        payload,
        key="m29-3-legal-guardian",
    )
    assert response.status_code == 201, response.data
    minor = patient_model().objects.exclude(pk=guardian.patient_profile.pk).get()
    child_card = document_model().objects.get(patient=minor)
    child_card.family_number = "FAM-100"
    child_card.save(update_fields=("family_number", "updated_at"))
    relationship = relationship_model().objects.latest("created_at")

    # Approval requires an identity-verification agent.
    from tests.factories import UserFactory

    agent = UserFactory(
        email="lg-agent@example.com",
        role="IDENTITY_VERIFICATION_AGENT",
        status="ACTIVE",
    )
    approve_identity_document(document=child_card, agent=agent)
    relationship.refresh_from_db()

    decision = can_approve_guardian_relationship(relationship)

    assert decision.name_evidence_kind is None
    assert decision.name_result == "UNAVAILABLE"
    assert decision.official_evidence_present is True
    # Legal guardian: official evidence + manual review; family/name match is
    # not required.
    assert decision.eligible is True


def test_mother_evidence_policy_version_bumped(api_client):
    _, _, _, relationship = _evaluated_mother(api_client)
    relationship.refresh_from_db()
    assert relationship.evidence_policy_version == EVIDENCE_POLICY_VERSION
    assert EVIDENCE_POLICY_VERSION == "M29_3_V1"


# --------------------------------------------------------------------------- #
# B. Child upload targets minor; revocation denies
# --------------------------------------------------------------------------- #

def _png_upload(color="teal"):
    output = io.BytesIO()
    Image.new("RGB", (2, 2), color).save(output, format="PNG")
    return SimpleUploadedFile(
        "child-report.png", output.getvalue(), content_type="image/png"
    )


def test_child_upload_targets_minor_not_guardian(api_client, tmp_path):
    guardian, minor, child_card, agent, relationship = pending_parent(api_client)
    approve_identity_document(document=child_card, agent=agent)
    relationship.refresh_from_db()
    approve_guardian_relationship(relationship=relationship, agent=agent)
    guardian_profile = guardian.patient_profile

    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        response = api_client.post(
            f"/api/v1/minors/{minor.uuid}/documents/",
            {"file": _png_upload(), "document_type": "MEDICAL_REPORT"},
            format="multipart",
        )

    assert response.status_code == 201, response.data
    document = minor.medical_documents.get()
    assert document.patient == minor
    assert document.uploaded_by == guardian
    assert document.patient != guardian_profile
    assert guardian_profile.medical_documents.filter(pk=document.pk).exists() is False


def test_revoked_relationship_denies_child_upload(api_client, tmp_path):
    guardian, minor, child_card, agent, relationship = pending_parent(api_client)
    approve_identity_document(document=child_card, agent=agent)
    relationship.refresh_from_db()
    approve_guardian_relationship(relationship=relationship, agent=agent)
    from guardians.services import revoke_guardian_relationship

    revoke_guardian_relationship(
        relationship=relationship, actor=guardian, reason="Synthetic revocation"
    )

    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        response = api_client.post(
            f"/api/v1/minors/{minor.uuid}/documents/",
            {"file": _png_upload(), "document_type": "MEDICAL_REPORT"},
            format="multipart",
        )

    assert response.status_code == 404
    assert minor.medical_documents.count() == 0
