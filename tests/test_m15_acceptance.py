"""
M15 — Full Workflow and Security Acceptance.

End-to-end acceptance proving the complete Phase 1 backend works as an
integrated system. No new product features; only minimal corrective fixes
if defects found.
"""

import io
from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image

from accounts.services import register_account
from audit.models import AuditLog
from claims.models import PatientAccountClaim
from claims.services.review import approve_account_claim, transition_claim
from claims.services.submission import submit_account_claim
from documents.models import MedicalDocument, StoredFile
from guardians.models import GuardianRelationship
from identities.models import IdentityDocument
from identities.services import (
    approve_identity_document,
    reject_identity_document,
    submit_identity_document,
)
from patients.models import PatientProfile

pytestmark = pytest.mark.django_db

User = get_user_model()

# ── helpers ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def private_identity_storage(settings, tmp_path):
    settings.IDENTITY_FILE_ROOT = tmp_path / "private-identity"
    settings.IDENTITY_FILE_MAX_BYTES = 1024 * 1024


def png(name="acceptance.png"):
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), (20, 30, 40)).save(stream, format="PNG")
    return SimpleUploadedFile(name, stream.getvalue(), content_type="image/png")


def agent_user(email="agent@example.com"):
    return User.objects.create_user(
        email=email,
        password="A-complex-password-2026!",
        role=User.Role.IDENTITY_VERIFICATION_AGENT,
        status=User.Status.ACTIVE,
    )


def create_verified_identity(profile, actor, doc_number="CARD-ACCEPT"):
    """Submit + approve National Card; return verified document."""
    doc = submit_identity_document(
        patient=profile,
        actor=actor,
        validated_data={
            "document_type": "UNIFIED_NATIONAL_CARD",
            "document_number": doc_number,
            "issuing_country": "IQ",
            "front_image": png("front.png"),
            "back_image": png("back.png"),
        },
    )
    agent = agent_user()
    approve_identity_document(document=doc, agent=agent)
    return doc


def create_medical_document_for_patient(patient, actor, api_client, tmp_path):
    """Upload medical PNG via API; return MedicalDocument."""
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        api_client.force_authenticate(user=actor)
        response = api_client.post(
            "/api/v1/documents/",
            {
                "document_type": "LABORATORY",
                "title": "M15 Acceptance Panel",
                "file": png("medical.png"),
            },
            format="multipart",
        )
    assert response.status_code == 201, response.content
    return MedicalDocument.objects.get(uuid=response.data["data"]["uuid"])


def set_document_verified_date(document):
    document.document_date = date(2026, 8, 1)
    document.date_verified = True
    document.date_source = MedicalDocument.DateSource.USER_ENTERED
    document.processing_status = MedicalDocument.ProcessingStatus.DATE_CONFIRMED
    document.save(
        update_fields=(
            "document_date",
            "date_verified",
            "date_source",
            "processing_status",
            "updated_at",
        )
    )


# ── A. Adult Lifecycle ───────────────────────────────────────────────


def test_full_adult_lifecycle(api_client, tmp_path):
    """Register → identity → medical → archive → audit."""
    user = register_account(
        email="m15-adult@example.com",
        password="A-complex-password-2026!",
        patient={
            "full_name": "M15 Adult Patient",
            "date_of_birth": date(1990, 5, 15),
            "sex": "MALE",
            "nationality": "IQ",
        },
    )
    profile = PatientProfile.objects.get(user=user)
    assert profile.digital_id is not None
    assert profile.identity_status == PatientProfile.IdentityStatus.UNVERIFIED

    create_verified_identity(profile, user, "CARD-M15A")
    profile.refresh_from_db()
    assert profile.identity_status == PatientProfile.IdentityStatus.VERIFIED

    document = create_medical_document_for_patient(profile, user, api_client, tmp_path)
    assert document.patient == profile
    set_document_verified_date(document)

    api_client.force_authenticate(user=user)
    archive = api_client.get("/api/v1/archive/")
    assert archive.status_code == 200
    uuids = {row["uuid"] for row in archive.data["data"]["results"]}
    assert str(document.uuid) in uuids
    assert api_client.get("/api/v1/archive/summary/").status_code == 200

    actions = set(
        AuditLog.objects.filter(patient=profile).values_list("action", flat=True)
    )
    for expected in (
        AuditLog.Action.ACCOUNT_CREATED,
        AuditLog.Action.IDENTITY_DOCUMENT_UPLOADED,
        AuditLog.Action.IDENTITY_DOCUMENT_VERIFIED,
        AuditLog.Action.DOCUMENT_UPLOADED,
    ):
        assert expected in actions, expected


# ── B. Identity Replacement ──────────────────────────────────────────


def test_identity_replacement_keeps_old_verified_until_new_approved():
    user = User.objects.create_user(
        email="m15-replace@example.com",
        password="A-complex-password-2026!",
        status=User.Status.ACTIVE,
    )
    profile = PatientProfile.objects.create(
        user=user,
        digital_id="12345678901234501",
        full_name="Replace Patient",
        date_of_birth=date(1990, 1, 1),
        sex="UNSPECIFIED",
        nationality="IQ",
    )
    doc = create_verified_identity(profile, user, "CARD-ORIG")
    profile.refresh_from_db()
    assert profile.identity_status == PatientProfile.IdentityStatus.VERIFIED

    replacement = submit_identity_document(
        patient=profile,
        actor=user,
        replaces=doc,
        validated_data={
            "document_type": "UNIFIED_NATIONAL_CARD",
            "document_number": "CARD-REPLACE",
            "issuing_country": "IQ",
            "front_image": png("replace-front.png"),
        },
    )
    doc.refresh_from_db()
    assert doc.status == IdentityDocument.LifecycleStatus.CURRENT
    assert doc.verification_status == IdentityDocument.VerificationStatus.VERIFIED

    agent = agent_user("agent-replace@example.com")
    reject_identity_document(document=replacement, agent=agent, reason="Invalid.")
    doc.refresh_from_db()
    assert doc.status == IdentityDocument.LifecycleStatus.CURRENT
    assert doc.verification_status == IdentityDocument.VerificationStatus.VERIFIED

    assert IdentityDocument.objects.filter(patient=profile).count() >= 2
    assert AuditLog.objects.filter(
        action=AuditLog.Action.IDENTITY_DOCUMENT_REJECTED, patient=profile
    ).exists()


# ── C. Parent/Minor Lifecycle ────────────────────────────────────────


def test_parent_minor_lifecycle(api_client, tmp_path):
    from guardians.services import approve_guardian_relationship
    from identities.services import approve_identity_document as approve_id
    from tests.test_minors_guardians import (
        birth_document_payload,
        create_verified_guardian,
        patient_model,
    )

    guardian, profile, agent = create_verified_guardian(
        email="m15-guardian@example.com", family="FAM-M15"
    )
    api_client.force_authenticate(user=guardian)
    minor_resp = api_client.post(
        "/api/v1/minors/",
        birth_document_payload(),
        format="multipart",
        HTTP_IDEMPOTENCY_KEY="m15-minor-1",
    )
    assert minor_resp.status_code == 201, minor_resp.content
    minor = patient_model().objects.exclude(pk=profile.pk).get()
    assert minor.digital_id is not None
    assert minor.pk != profile.pk

    # Approve minor's identity + relationship so guardian can upload.
    approve_id(document=IdentityDocument.objects.get(patient=minor), agent=agent)
    relationship = GuardianRelationship.objects.get(minor_patient=minor)
    approve_guardian_relationship(relationship=relationship, agent=agent)

    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        upload_resp = api_client.post(
            f"/api/v1/minors/{minor.uuid}/documents/",
            {
                "document_type": "LABORATORY",
                "title": "Minor Report",
                "file": png("minor-doc.png"),
            },
            format="multipart",
        )
    assert upload_resp.status_code == 201, upload_resp.content
    doc_uuid = upload_resp.data["data"]["uuid"]
    document = MedicalDocument.objects.get(uuid=doc_uuid)
    assert document.patient == minor

    archive = api_client.get(f"/api/v1/minors/{minor.uuid}/archive/")
    assert archive.status_code == 200

    assert AuditLog.objects.filter(
        action=AuditLog.Action.MINOR_CREATED, patient=minor, actor=guardian
    ).exists()


# ── D. Adult Claim + Activation ──────────────────────────────────────


def test_adult_claim_and_activation_preserves_existing_profile():
    from tests.test_account_claiming import image_upload as claim_img
    from tests.test_account_claiming import verified_adult

    profile = verified_adult()
    original_uuid = profile.uuid
    pre_count = PatientProfile.objects.count()

    receipt = submit_account_claim(
        {
            "digital_id": "12345678901234567",
            "email": "m15-claimant@example.com",
            "phone": "+9647700000000",
            "full_name": "M15 Claimant",
            "date_of_birth": date(1990, 1, 2),
            "identity_document_number": "CARD-001",
            "front_image": claim_img("m15-front.png"),
            "back_image": claim_img("m15-back.png"),
        }
    )
    claim = PatientAccountClaim.objects.get(uuid=receipt.claim_id)
    agent = agent_user("m15-claim-agent@example.com")
    approved = approve_account_claim(claim=claim, agent=agent)

    profile.refresh_from_db()
    assert profile.uuid == original_uuid
    assert PatientProfile.objects.count() == pre_count
    assert profile.user is not None
    assert profile.user.email == "m15-claimant@example.com"

    from claims.services.activation import activate_claimed_account

    activate_claimed_account(
        token=approved.activation_token,
        new_password="A-complex-password-2026!",
    )
    profile.user.refresh_from_db()
    assert profile.user.status == User.Status.ACTIVE

    actions = set(
        AuditLog.objects.filter(patient=profile).values_list("action", flat=True)
    )
    for expected in (
        AuditLog.Action.CLAIM_SUBMITTED,
        AuditLog.Action.CLAIM_APPROVED,
        AuditLog.Action.PATIENT_ACCOUNT_LINKED,
        AuditLog.Action.ACCOUNT_ACTIVATED,
    ):
        assert expected in actions, expected


# ── E. Claim Rejection ───────────────────────────────────────────────


def test_claim_rejection_preserves_data():
    from tests.test_account_claiming import image_upload as claim_img
    from tests.test_account_claiming import verified_adult

    profile = verified_adult(digital_id="12345678901234568")
    original_user_id = profile.user_id

    receipt = submit_account_claim(
        {
            "digital_id": "12345678901234568",
            "email": "m15-rejected@example.com",
            "phone": "+9647700000001",
            "full_name": "M15 Rejected",
            "date_of_birth": date(1990, 1, 2),
            "identity_document_number": "CARD-001",
            "front_image": claim_img("r-front.png"),
            "back_image": claim_img("r-back.png"),
        }
    )
    claim = PatientAccountClaim.objects.get(uuid=receipt.claim_id)
    agent = agent_user("m15-reject-agent@example.com")
    transition_claim(
        claim=claim,
        agent=agent,
        status=PatientAccountClaim.Status.REJECTED,
        reason="Not valid.",
    )
    claim.refresh_from_db()
    assert claim.status == PatientAccountClaim.Status.REJECTED

    profile.refresh_from_db()
    assert profile.user_id == original_user_id
    assert claim.status == PatientAccountClaim.Status.REJECTED
    assert AuditLog.objects.filter(
        action=AuditLog.Action.CLAIM_REJECTED, resource_uuid=claim.uuid
    ).exists()


# ── F. Integrity Flows ───────────────────────────────────────────────


def test_integrity_corruption_blocks_download_and_audits(api_client, tmp_path):
    from tests.test_medical_documents_api import patient_user

    user, patient = patient_user(email="m15-integrity@example.com")
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        document = create_medical_document_for_patient(
            patient, user, api_client, tmp_path
        )
        stored = document.stored_file
        assert stored.integrity_status == StoredFile.IntegrityStatus.VALID

        with stored.file.open("rb+") as f:
            f.seek(0)
            f.write(b"X")

        from documents.services import verify_stored_file_integrity

        verified = verify_stored_file_integrity(stored, actor=user)
        assert verified.integrity_status == StoredFile.IntegrityStatus.CORRUPTED

        api_client.force_authenticate(user=user)
        download = api_client.get(f"/api/v1/documents/{document.uuid}/file/")
        assert download.status_code == 409

        assert AuditLog.objects.filter(
            action=AuditLog.Action.INTEGRITY_FAILURE,
            resource_uuid=document.uuid,
        ).exists()


def test_missing_blob_marks_missing_and_audits(api_client, tmp_path):
    from tests.test_medical_documents_api import patient_user

    user, patient = patient_user(email="m15-missing@example.com")
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        document = create_medical_document_for_patient(
            patient, user, api_client, tmp_path
        )
        stored = document.stored_file
        stored.file.storage.delete(stored.file.name)

        from documents.services import verify_stored_file_integrity

        verified = verify_stored_file_integrity(stored, actor=user)
        assert verified.integrity_status == StoredFile.IntegrityStatus.MISSING

        api_client.force_authenticate(user=user)
        download = api_client.get(f"/api/v1/documents/{document.uuid}/file/")
        assert download.status_code == 409

        assert AuditLog.objects.filter(
            action=AuditLog.Action.INTEGRITY_FAILURE,
            resource_uuid=document.uuid,
        ).exists()


# ── G. Soft Delete ───────────────────────────────────────────────────


def test_soft_delete_removes_from_public_but_preserves_internal(api_client, tmp_path):
    from tests.test_medical_documents_api import COLLECTION, patient_user

    user, patient = patient_user(email="m15-softdel@example.com")
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        document = create_medical_document_for_patient(
            patient, user, api_client, tmp_path
        )
    set_document_verified_date(document)
    doc_uuid = str(document.uuid)

    api_client.force_authenticate(user=user)
    assert api_client.delete(f"{COLLECTION}{doc_uuid}/").status_code == 204

    assert api_client.get(f"{COLLECTION}{doc_uuid}/").status_code == 404
    archive = api_client.get("/api/v1/archive/")
    assert all(row["uuid"] != doc_uuid for row in archive.data["data"]["results"])

    document.refresh_from_db()
    assert document.archive_status == MedicalDocument.ArchiveStatus.DELETED
    assert AuditLog.objects.filter(
        action=AuditLog.Action.DOCUMENT_DELETED,
        resource_uuid=document.uuid,
    ).exists()


# ── H. Security: IDOR Matrix ─────────────────────────────────────────


def test_patient_isolation_medical_document(api_client, tmp_path):
    from tests.test_medical_documents_api import COLLECTION, patient_user

    owner, patient_a = patient_user(email="m15-pt-a@example.com")
    other, patient_b = patient_user(
        email="m15-pt-b@example.com", digital_id="76543210987654321"
    )
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        doc_a = create_medical_document_for_patient(
            patient_a, owner, api_client, tmp_path
        )
    detail_url = f"{COLLECTION}{doc_a.uuid}/"

    api_client.force_authenticate(user=owner)
    assert api_client.get(detail_url).status_code == 200

    api_client.force_authenticate(user=other)
    assert api_client.get(detail_url).status_code == 404

    api_client.force_authenticate()
    assert api_client.get(detail_url).status_code == 401


def test_verification_agent_cannot_access_medical_data(api_client, tmp_path):
    from tests.test_medical_documents_api import patient_user

    user, patient = patient_user(email="m15-agent-block@example.com")
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        document = create_medical_document_for_patient(
            patient, user, api_client, tmp_path
        )
    detail_url = f"/api/v1/documents/{document.uuid}/"
    agent = agent_user("m15-agent-test@example.com")
    api_client.force_authenticate(user=agent)

    assert api_client.get(detail_url).status_code in (403, 404)
    assert api_client.get(f"{detail_url}file/").status_code in (403, 404)
    assert api_client.get("/api/v1/archive/").status_code in (403, 404)


# ── I. Audit Privacy ─────────────────────────────────────────────────


def test_audit_payloads_contain_no_secrets():
    """All existing audit rows must not leak credentials/paths/raw text."""
    forbidden = (
        "password",
        "token",
        "activation",
        "sha256",
        "storage_key",
        "private/identity",
        "private/medical",
    )
    for entry in AuditLog.objects.all():
        blob = str(entry.previous_values) + str(entry.new_values) + str(entry.metadata)
        for term in forbidden:
            assert term.lower() not in blob.lower(), (
                f"AuditLog {entry.uuid} leaks {term!r}"
            )


# ── J. Error Envelope ────────────────────────────────────────────────


def test_error_envelope_consistency(api_client):
    from tests.test_medical_documents_api import COLLECTION, patient_user

    # 401.
    resp = api_client.get(COLLECTION)
    assert resp.status_code == 401
    assert set(resp.json()) == {"error"}
    assert set(resp.json()["error"]) == {"code", "message", "details"}

    # 404.
    user, _ = patient_user(email="m15-envelope@example.com")
    api_client.force_authenticate(user=user)
    resp = api_client.get(f"{COLLECTION}00000000-0000-0000-0000-000000000000/file/")
    assert resp.status_code == 404
    assert set(resp.json()) == {"error"}
    assert "Traceback" not in resp.content.decode()


# ── K. Mass Assignment ───────────────────────────────────────────────


def test_upload_rejects_protected_fields(api_client, tmp_path):
    from tests.test_medical_documents_api import COLLECTION, patient_user

    user, _ = patient_user(email="m15-mass@example.com")
    api_client.force_authenticate(user=user)
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        for protected in (
            {"patient": "00000000-0000-0000-0000-000000000000"},
            {"processing_status": "INDEXED"},
            {"archive_status": "DELETED"},
            {"date_verified": True},
        ):
            resp = api_client.post(
                COLLECTION,
                {"document_type": "LABORATORY", "file": png(), **protected},
                format="multipart",
            )
            assert resp.status_code == 400, protected


# ── L. Duplicate Upload ──────────────────────────────────────────────


def test_duplicate_same_bytes_rejected(api_client, tmp_path):
    from tests.test_medical_documents_api import COLLECTION, patient_user

    user, patient = patient_user(email="m15-dup@example.com")
    api_client.force_authenticate(user=user)
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        first = api_client.post(
            COLLECTION,
            {"document_type": "LABORATORY", "file": png("same.png")},
            format="multipart",
        )
        assert first.status_code == 201

        second = api_client.post(
            COLLECTION,
            {"document_type": "LABORATORY", "file": png("same.png")},
            format="multipart",
        )
        assert second.status_code == 409
    assert MedicalDocument.objects.filter(patient=patient).count() == 1
