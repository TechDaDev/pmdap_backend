"""Scan-first registration tests — SYNTHETIC data only.

Every identity value used here is invented (SYNTHNAME / TESTFAMILY123456 /
H12345678 / 999999999999 / 1990-05-17 ...). Real owner card values never
appear. OCR is faked: ``registration.tasks._run_ocr_and_extract`` returns a
synthetic payload, so no PaddleOCR runs.
"""
import hashlib
import io

import pytest
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image

from accounts.models import User
from identities.models import IdentityDocument, IdentityFile
from patients.models import PatientProfile
from registration.models import RegistrationIdentityExtractionJob
from registration.services import issue_registration_job

EXTRACT = "/api/v1/auth/register/identity/extract/"
REGISTER = "/api/v1/auth/register/"


def synthetic_png(text="SYNTHETIC"):
    img = Image.new("RGB", (420, 140), "white")
    from PIL import ImageDraw

    ImageDraw.Draw(img).text((10, 10), text, fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return SimpleUploadedFile("synthetic.png", buf.getvalue(), content_type="image/png")


def synthetic_payload():
    return {
        "document_type": "UNIFIED_NATIONAL_CARD",
        "extractor_version": "identity-v1",
        "fields": {
            "name": {"value": "SYNTHNAME", "confidence": 0.9, "source": "FRONT_PRINTED"},
            "national_card_number": {
                "value": "999999999999",
                "confidence": 0.95,
                "source": "FRONT_PRINTED",
            },
            "document_number": {
                "value": "999999999999",
                "confidence": 0.95,
                "source": "FRONT_PRINTED",
            },
            "unique_card_body_number": {
                "value": "H12345678",
                "confidence": 0.95,
                "source": "FRONT_PRINTED",
            },
            "family_number": {
                "value": "TESTFAMILY123456",
                "confidence": 0.85,
                "source": "BACK_PRINTED",
            },
            "date_of_birth": {
                "value": "1990-05-17",
                "confidence": 0.94,
                "source": "BACK_PRINTED",
            },
            "sex": {"value": "MALE", "confidence": 0.96, "source": "FRONT_PRINTED"},
            "blood_group": {"value": "O+", "confidence": 0.82, "source": "ROI"},
            "issuing_country": {
                "value": "IQ",
                "confidence": 1.0,
                "source": "DOCUMENT_TYPE",
            },
        },
        "warnings": [],
        "mrz": {"detected": True, "valid": False, "checks_passed": False},
    }


def confirmed_identity(job_id, token):
    return {
        "job_id": str(job_id),
        "job_token": token,
        "document_type": "UNIFIED_NATIONAL_CARD",
        "document_number": "999999999999",
        "national_card_number": "999999999999",
        "family_number": "TESTFAMILY123456",
        "unique_card_body_number": "H12345678",
        "name": "SYNTHNAME",
        "father_name": "SYNTHFATHER",
        "grandfather_name": "SYNTHGRANDFATHER",
        "confirmation": True,
        "date_of_birth": "1990-05-17",
        "sex": "MALE",
        "nationality": "IQ",
        "blood_group": "O+",
    }


@pytest.fixture(autouse=True)
def eager_ocr(monkeypatch):
    """Run the worker eagerly and fake the OCR+extraction core."""

    def fake_run_ocr_and_extract(*, front_key, back_key, document_type, total_started):
        return synthetic_payload(), {"total_ms": 1}, 5

    monkeypatch.setattr(
        "registration.tasks._run_ocr_and_extract", fake_run_ocr_and_extract
    )
    monkeypatch.setattr(
        "identities.tasks._run_ocr_and_extract", fake_run_ocr_and_extract
    )


@pytest.fixture(autouse=True)
def eager_settings(settings):
    settings.CELERY_TASK_ALWAYS_EAGER = True
    settings.CELERY_TASK_EAGER_PROPAGATES = True
    settings.REGISTRATION_IDENTITY_TTL_SECONDS = 30 * 60
    settings.REGISTRATION_IDENTITY_CACHE_TTL_SECONDS = 30 * 60


def _auth(user):
    from rest_framework_simplejwt.tokens import RefreshToken

    return f"Bearer {RefreshToken.for_user(user).access_token}"


# --------------------------------------------------------------------------- #
# PUBLIC EXTRACTION
# --------------------------------------------------------------------------- #

@pytest.mark.django_db
def test_extract_public_no_jwt_required(api_client):
    resp = api_client.post(
        EXTRACT,
        {
            "document_type": "UNIFIED_NATIONAL_CARD",
            "front_image": synthetic_png(),
            "back_image": synthetic_png(),
        },
    )
    assert resp.status_code == 202
    data = resp.json()["data"]
    assert data["status"] == "PENDING"
    assert data["job_id"]
    assert len(data["job_token"]) >= 32


@pytest.mark.django_db
def test_extract_rejects_bad_image(api_client):
    bad = SimpleUploadedFile(
        "bad.txt", b"not an image", content_type="text/plain"
    )
    resp = api_client.post(
        EXTRACT,
        {
            "document_type": "UNIFIED_NATIONAL_CARD",
            "front_image": bad,
            "back_image": synthetic_png(),
        },
    )
    assert resp.status_code == 400


@pytest.mark.django_db
def test_extract_phase1_rejects_passport(api_client):
    resp = api_client.post(
        EXTRACT,
        {
            "document_type": "PASSPORT",
            "front_image": synthetic_png(),
            "back_image": synthetic_png(),
        },
    )
    assert resp.status_code == 400


@pytest.mark.django_db
def test_extract_back_required(api_client):
    resp = api_client.post(
        EXTRACT,
        {"document_type": "UNIFIED_NATIONAL_CARD", "front_image": synthetic_png()},
    )
    assert resp.status_code == 400


def test_extract_throttle_scope(api_client):
    from registration.api import RegistrationIdentityExtractView

    view = RegistrationIdentityExtractView()
    assert view.throttle_scope == "registration_identity_extract"


# --------------------------------------------------------------------------- #
# CAPABILITY
# --------------------------------------------------------------------------- #

@pytest.mark.django_db
def test_token_not_stored_plaintext(api_client):
    resp = api_client.post(
        EXTRACT,
        {
            "document_type": "UNIFIED_NATIONAL_CARD",
            "front_image": synthetic_png(),
            "back_image": synthetic_png(),
        },
    )
    data = resp.json()["data"]
    job = RegistrationIdentityExtractionJob.objects.get(uuid=data["job_id"])
    assert job.capability_digest != data["job_token"]
    assert len(job.capability_digest) == 64
    assert job.capability_digest == hashlib.sha256(
        data["job_token"].encode()
    ).hexdigest()


@pytest.mark.django_db
def test_poll_requires_token(api_client):
    resp = api_client.post(
        EXTRACT,
        {
            "document_type": "UNIFIED_NATIONAL_CARD",
            "front_image": synthetic_png(),
            "back_image": synthetic_png(),
        },
    )
    job_id = resp.json()["data"]["job_id"]
    resp = api_client.get(f"{EXTRACT}{job_id}/")
    assert resp.status_code == 404  # no existence leak


@pytest.mark.django_db
def test_poll_wrong_token_denied_no_leak(api_client):
    resp = api_client.post(
        EXTRACT,
        {
            "document_type": "UNIFIED_NATIONAL_CARD",
            "front_image": synthetic_png(),
            "back_image": synthetic_png(),
        },
    )
    job_id = resp.json()["data"]["job_id"]
    resp = api_client.get(
        f"{EXTRACT}{job_id}/", headers={"X-Registration-Job-Token": "wrong"}
    )
    assert resp.status_code == 404
    # Unknown job also 404s identically.
    import uuid as uuid_mod

    resp = api_client.get(
        f"{EXTRACT}{uuid_mod.uuid4()}/",
        headers={"X-Registration-Job-Token": "wrong"},
    )
    assert resp.status_code == 404


@pytest.mark.django_db
def test_poll_other_job_token_denied(api_client):
    def create():
        r = api_client.post(
            EXTRACT,
            {
                "document_type": "UNIFIED_NATIONAL_CARD",
                "front_image": synthetic_png(),
                "back_image": synthetic_png(),
            },
        )
        return r.json()["data"]

    a = create()
    b = create()
    resp = api_client.get(
        f"{EXTRACT}{a['job_id']}/",
        headers={"X-Registration-Job-Token": b["job_token"]},
    )
    assert resp.status_code == 404


@pytest.mark.django_db
def test_poll_correct_token_success(api_client):
    resp = api_client.post(
        EXTRACT,
        {
            "document_type": "UNIFIED_NATIONAL_CARD",
            "front_image": synthetic_png(),
            "back_image": synthetic_png(),
        },
    )
    data = resp.json()["data"]
    resp = api_client.get(
        f"{EXTRACT}{data['job_id']}/",
        headers={"X-Registration-Job-Token": data["job_token"]},
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["status"] == "SUCCESS"
    assert body["job_id"] == data["job_id"]
    # Advisory payload present (synthetic), never raw OCR text.
    assert "national_card_number" in body["fields"]
    assert body["fields"]["unique_card_body_number"]["value"] == "H12345678"
    assert "job_token" not in body


# --------------------------------------------------------------------------- #
# TTL / CLEANUP
# --------------------------------------------------------------------------- #

@pytest.mark.django_db
def test_expired_job_poll_410(api_client):
    resp = api_client.post(
        EXTRACT,
        {
            "document_type": "UNIFIED_NATIONAL_CARD",
            "front_image": synthetic_png(),
            "back_image": synthetic_png(),
        },
    )
    data = resp.json()["data"]
    job = RegistrationIdentityExtractionJob.objects.get(uuid=data["job_id"])
    job.status = RegistrationIdentityExtractionJob.Status.SUCCESS
    job.expires_at = None
    job.save(update_fields=["status", "expires_at"])
    # Simulate expiry by back-dating created_at beyond TTL is complex; instead
    # flip status to EXPIRED directly (poll path maps EXPIRED -> 410).
    job.status = RegistrationIdentityExtractionJob.Status.EXPIRED
    job.save(update_fields=["status"])
    resp = api_client.get(
        f"{EXTRACT}{data['job_id']}/",
        headers={"X-Registration-Job-Token": data["job_token"]},
    )
    assert resp.status_code == 410


@pytest.mark.django_db
def test_cleanup_sweep_removes_expired(api_client):
    resp = api_client.post(
        EXTRACT,
        {
            "document_type": "UNIFIED_NATIONAL_CARD",
            "front_image": synthetic_png(),
            "back_image": synthetic_png(),
        },
    )
    data = resp.json()["data"]
    from django.utils import timezone
    from datetime import timedelta

    job = RegistrationIdentityExtractionJob.objects.get(uuid=data["job_id"])
    job.expires_at = timezone.now() - timedelta(seconds=1)
    job.save(update_fields=["expires_at"])

    from registration.tasks import cleanup_registration_identity_jobs

    cleanup_registration_identity_jobs()
    assert not RegistrationIdentityExtractionJob.objects.filter(
        uuid=data["job_id"]
    ).exists()


# --------------------------------------------------------------------------- #
# FINAL REGISTER
# --------------------------------------------------------------------------- #

def _successful_job(api_client):
    resp = api_client.post(
        EXTRACT,
        {
            "document_type": "UNIFIED_NATIONAL_CARD",
            "front_image": synthetic_png(),
            "back_image": synthetic_png(),
        },
    )
    data = resp.json()["data"]
    job = RegistrationIdentityExtractionJob.objects.get(uuid=data["job_id"])
    assert job.status == RegistrationIdentityExtractionJob.Status.SUCCESS
    return data["job_id"], data["job_token"]


def _register(
    api_client,
    job_id,
    token,
    *,
    email="synth.reg@example.invalid",
    password="StrongPass123!",
    **overrides,
):
    payload = confirmed_identity(job_id, token)
    payload.update(overrides)
    return api_client.post(
        REGISTER,
        {
            "email": email,
            "password": password,
            "governorate": "BAGHDAD",
            "registration_identity": payload,
        },
        format="json",
    )


@pytest.mark.django_db
def test_scan_first_register_full_lifecycle(api_client):
    resp = api_client.post(
        EXTRACT,
        {
            "document_type": "UNIFIED_NATIONAL_CARD",
            "front_image": synthetic_png(),
            "back_image": synthetic_png(),
        },
    )
    data = resp.json()["data"]
    job = RegistrationIdentityExtractionJob.objects.get(uuid=data["job_id"])
    assert job.status == RegistrationIdentityExtractionJob.Status.SUCCESS
    front_key, back_key = job.front_key, job.back_key

    resp = _register(api_client, data["job_id"], data["job_token"])
    assert resp.status_code == 201, resp.content

    user = User.objects.get(email="synth.reg@example.invalid")
    assert user.status == User.Status.ACTIVE
    assert user.check_password("StrongPass123!")
    profile = PatientProfile.objects.get(user=user)
    assert profile.digital_id.startswith("PT-")
    assert profile.identity_status == PatientProfile.IdentityStatus.PENDING_VERIFICATION
    assert profile.blood_group == "O+"
    # Structured patronymic components + governorate persisted.
    assert profile.full_name == "SYNTHNAME SYNTHFATHER SYNTHGRANDFATHER"
    assert profile.father_name == "SYNTHFATHER"
    assert profile.grandfather_name == "SYNTHGRANDFATHER"
    assert profile.governorate == "BAGHDAD"

    doc = IdentityDocument.objects.get(patient=profile)
    assert doc.verification_status == IdentityDocument.VerificationStatus.PENDING
    assert doc.status == IdentityDocument.LifecycleStatus.CURRENT
    assert doc.document_number == "999999999999"
    assert doc.family_number == "TESTFAMILY123456"
    assert doc.unique_card_body_number == "H12345678"
    assert doc.issuing_country == "IQ"
    assert IdentityFile.objects.count() == 2

    # Events + audit recorded.
    from identities.models import IdentityDocumentEvent
    from audit.models import AuditLog

    assert IdentityDocumentEvent.objects.filter(document=doc).exists()
    assert AuditLog.objects.filter(action=AuditLog.Action.ACCOUNT_CREATED).exists()
    assert AuditLog.objects.filter(
        action=AuditLog.Action.PATIENT_PROFILE_CREATED
    ).exists()
    assert AuditLog.objects.filter(
        action=AuditLog.Action.IDENTITY_DOCUMENT_UPLOADED
    ).exists()

    # Least-disclosure response: patient/identity summary, NO card identifiers.
    body = resp.json()["data"]
    assert body["patient"]["uuid"] == str(profile.uuid)
    assert body["patient"]["identity_status"] == "PENDING_VERIFICATION"
    assert body["identity_document"]["verification_status"] == "PENDING"
    assert "family_number" not in body["patient"]
    assert "national_card_number" not in body["patient"]

    # Job consumed (FINALIZED) atomically with the account. Row/staging
    # cleanup is post-commit (on_commit); invoke it directly here since
    # pytest-django rolls the outer transaction back.
    job = RegistrationIdentityExtractionJob.objects.get(uuid=data["job_id"])
    assert job.status == RegistrationIdentityExtractionJob.Status.FINALIZED
    from registration.services import cleanup_registration_job_after_finalize

    cleanup_registration_job_after_finalize(job)
    assert not RegistrationIdentityExtractionJob.objects.filter(
        uuid=data["job_id"]
    ).exists()
    from identities.storage import private_identity_storage

    for key in (front_key, back_key):
        assert not private_identity_storage.exists(key)


@pytest.mark.django_db
def test_job_replay_denied_after_success(api_client):
    job_id, token = _successful_job(api_client)
    assert _register(api_client, job_id, token).status_code == 201
    # Second attempt with the same (now consumed) capability -> 409/404.
    resp = _register(api_client, job_id, token, email="other@example.invalid")
    assert resp.status_code in (409, 404)


@pytest.mark.django_db
def test_duplicate_email_does_not_consume_job(api_client):
    job_id, token = _successful_job(api_client)
    assert _register(api_client, job_id, token).status_code == 201

    # Fresh job; register with the duplicate email -> 400, job NOT consumed.
    job2_id, token2 = _successful_job(api_client)
    resp = _register(api_client, job2_id, token2, email="synth.reg@example.invalid")
    assert resp.status_code == 400
    job2 = RegistrationIdentityExtractionJob.objects.get(uuid=job2_id)
    assert job2.status != RegistrationIdentityExtractionJob.Status.FINALIZED
    assert User.objects.filter(email="synth.reg@example.invalid").count() == 1


@pytest.mark.django_db
def test_under18_dob_rejected(api_client):
    job_id, token = _successful_job(api_client)
    resp = _register(api_client, job_id, token, date_of_birth="2014-03-24")
    assert resp.status_code == 400
    job = RegistrationIdentityExtractionJob.objects.get(uuid=job_id)
    assert job.status != RegistrationIdentityExtractionJob.Status.FINALIZED


@pytest.mark.django_db
def test_bad_confirmed_fields_do_not_consume_job(api_client):
    job_id, token = _successful_job(api_client)
    resp = _register(api_client, job_id, token, nationality="XYZ")
    assert resp.status_code == 400
    assert RegistrationIdentityExtractionJob.objects.get(
        uuid=job_id
    ).status != RegistrationIdentityExtractionJob.Status.FINALIZED


@pytest.mark.django_db
def test_legacy_patient_register_still_works(api_client):
    resp = api_client.post(
        REGISTER,
        {
            "email": "legacy@example.invalid",
            "password": "StrongPass123!",
            "patient": {
                "full_name": "LEGACY NAME",
                "date_of_birth": "1990-01-01",
                "sex": "FEMALE",
                "nationality": "IQ",
            },
        },
        format="json",
    )
    assert resp.status_code == 201
    assert User.objects.filter(email="legacy@example.invalid").exists()


@pytest.mark.django_db
def test_both_contracts_rejected(api_client):
    resp = api_client.post(
        REGISTER,
        {
            "email": "both@example.invalid",
            "password": "StrongPass123!",
            "patient": {
                "full_name": "N",
                "date_of_birth": "1990-01-01",
                "sex": "MALE",
                "nationality": "IQ",
            },
            "registration_identity": {
                "job_id": "00000000-0000-0000-0000-000000000000",
                "job_token": "t",
            },
        },
        format="json",
    )
    assert resp.status_code == 400


@pytest.mark.django_db
def test_storage_promotion_failure_no_partial_account(api_client, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("storage down")

    monkeypatch.setattr("registration.services._create_document", boom)
    job_id, token = _successful_job(api_client)
    with pytest.raises(RuntimeError):
        _register(api_client, job_id, token)
    # No partial account/profile; job NOT consumed; staging preserved.
    assert not User.objects.filter(email="synth.reg@example.invalid").exists()
    assert not PatientProfile.objects.exists()
    job = RegistrationIdentityExtractionJob.objects.get(uuid=job_id)
    assert job.status != RegistrationIdentityExtractionJob.Status.FINALIZED


@pytest.mark.django_db
def test_wrong_document_type_mismatch(api_client):
    job_id, token = _successful_job(api_client)
    resp = _register(api_client, job_id, token, document_type="PASSPORT")
    assert resp.status_code in (400, 409)
    assert RegistrationIdentityExtractionJob.objects.get(
        uuid=job_id
    ).status != RegistrationIdentityExtractionJob.Status.FINALIZED


@pytest.mark.django_db
def test_four_identifiers_stay_distinct(api_client):
    job_id, token = _successful_job(api_client)
    payload = confirmed_identity(job_id, token)
    payload["document_number"] = "DOC-ALPHA-1"
    payload["national_card_number"] = "NAT-BETA-2"
    payload["family_number"] = "FAM-GAMMA-3"
    payload["unique_card_body_number"] = "H-DELTA-4"
    resp = api_client.post(
        REGISTER,
        {
            "email": "distinct@example.invalid",
            "password": "StrongPass123!",
            "governorate": "BASRA",
            "registration_identity": payload,
        },
        format="json",
    )
    assert resp.status_code == 201
    doc = IdentityDocument.objects.get(
        patient__user__email="distinct@example.invalid"
    )
    assert doc.document_number == "DOC-ALPHA-1"
    assert doc.national_number == "NAT-BETA-2"
    assert doc.family_number == "FAM-GAMMA-3"
    assert doc.unique_card_body_number == "H-DELTA-4"
    assert doc.patient.governorate == "BASRA"


@pytest.mark.django_db
def test_governorate_required_for_scan_first(api_client):
    job_id, token = _successful_job(api_client)
    resp = api_client.post(
        REGISTER,
        {
            "email": "nogovern@example.invalid",
            "password": "StrongPass123!",
            "registration_identity": confirmed_identity(job_id, token),
        },
        format="json",
    )
    assert resp.status_code == 400
    assert RegistrationIdentityExtractionJob.objects.get(
        uuid=job_id
    ).status != RegistrationIdentityExtractionJob.Status.FINALIZED


@pytest.mark.django_db
def test_confirmation_required_and_must_be_true(api_client):
    job_id, token = _successful_job(api_client)
    payload = confirmed_identity(job_id, token)
    payload["confirmation"] = False
    resp = api_client.post(
        REGISTER,
        {
            "email": "noconfirm@example.invalid",
            "password": "StrongPass123!",
            "governorate": "ERBIL",
            "registration_identity": payload,
        },
        format="json",
    )
    assert resp.status_code == 400
    job = RegistrationIdentityExtractionJob.objects.get(uuid=job_id)
    assert job.status != RegistrationIdentityExtractionJob.Status.FINALIZED


@pytest.mark.django_db
def test_empty_structured_name_rejected(api_client):
    job_id, token = _successful_job(api_client)
    resp = _register(api_client, job_id, token, name="   ")
    assert resp.status_code == 400
    assert RegistrationIdentityExtractionJob.objects.get(
        uuid=job_id
    ).status != RegistrationIdentityExtractionJob.Status.FINALIZED


@pytest.mark.django_db
def test_duplicate_national_card_number_rejected(api_client):
    job_id, token = _successful_job(api_client)
    assert _register(api_client, job_id, token).status_code == 201

    # Same card number on a fresh job -> safe conflict, no account, job kept.
    job2_id, token2 = _successful_job(api_client)
    resp = _register(api_client, job2_id, token2, email="dup@example.invalid")
    assert resp.status_code == 400
    assert not User.objects.filter(email="dup@example.invalid").exists()
    assert RegistrationIdentityExtractionJob.objects.get(
        uuid=job2_id
    ).status != RegistrationIdentityExtractionJob.Status.FINALIZED


@pytest.mark.django_db
def test_duplicate_card_body_number_rejected(api_client):
    job_id, token = _successful_job(api_client)
    assert _register(api_client, job_id, token).status_code == 201

    job2_id, token2 = _successful_job(api_client)
    payload = confirmed_identity(job2_id, token2)
    payload["document_number"] = "777777777777"
    payload["national_card_number"] = "777777777777"
    # Same physical body number but a different card number -> conflict on body.
    resp = api_client.post(
        REGISTER,
        {
            "email": "dupbody@example.invalid",
            "password": "StrongPass123!",
            "governorate": "NAJAF",
            "registration_identity": payload,
        },
        format="json",
    )
    assert resp.status_code == 400
    assert not User.objects.filter(email="dupbody@example.invalid").exists()


@pytest.mark.django_db
def test_family_number_duplicates_allowed_no_relationship(api_client):
    from guardians.models import GuardianRelationship

    job_id, token = _successful_job(api_client)
    resp_a = _register(api_client, job_id, token, email="famA@example.invalid")
    assert resp_a.status_code == 201

    job2_id, token2 = _successful_job(api_client)
    payload = confirmed_identity(job2_id, token2)
    payload["document_number"] = "888888888888"
    payload["national_card_number"] = "888888888888"
    payload["unique_card_body_number"] = "H88888888"
    resp_b = api_client.post(
        REGISTER,
        {
            "email": "famB@example.invalid",
            "password": "StrongPass123!",
            "governorate": "BAGHDAD",
            "registration_identity": payload,
        },
        format="json",
    )
    # Same family number (TESTFAMILY123456) is allowed for both profiles.
    assert resp_b.status_code == 201
    doc_a = IdentityDocument.objects.get(
        patient__user__email__iexact="famA@example.invalid"
    )
    doc_b = IdentityDocument.objects.get(
        patient__user__email__iexact="famB@example.invalid"
    )
    assert doc_a.family_number == doc_b.family_number == "TESTFAMILY123456"
    # No family/guardian relationship is created in Step 2.
    assert GuardianRelationship.objects.count() == 0


@pytest.mark.django_db
def test_arabic_names_alphanumeric_family_and_g_body_accepted(api_client):
    """Finalization accepts real-card-shaped data (SYNTHETIC values).

    Arabic name components, an alphanumeric family number, and a G-prefix
    body number must all pass validation and persist unchanged. Password is
    stored hashed, never plaintext.
    """
    job_id, token = _successful_job(api_client)
    payload = confirmed_identity(job_id, token)
    payload.update(
        {
            "name": "اسماعيل",
            "father_name": "عواد",
            "grandfather_name": "احمد",
            "family_number": "1012L0M10290019303",
            "unique_card_body_number": "G12345678",
            "national_card_number": "198060266608",
            "document_number": "198060266608",
        }
    )
    resp = api_client.post(
        REGISTER,
        {
            "email": "arabic.synth@example.invalid",
            "password": "StrongPass123!",
            "governorate": "NAJAF",
            "registration_identity": payload,
        },
        format="json",
    )
    assert resp.status_code == 201, resp.content

    user = User.objects.get(email="arabic.synth@example.invalid")
    assert user.check_password("StrongPass123!")
    assert user.password != "StrongPass123!"
    profile = user.patient_profile
    assert profile.full_name == "اسماعيل عواد احمد"
    assert profile.father_name == "عواد"
    assert profile.grandfather_name == "احمد"
    assert profile.governorate == "NAJAF"
    doc = IdentityDocument.objects.get(patient=profile)
    assert doc.document_number == "198060266608"
    assert doc.family_number == "1012L0M10290019303"
    assert doc.unique_card_body_number == "G12345678"
