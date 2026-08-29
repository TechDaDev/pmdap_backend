"""M31B registration email-verification tests — SYNTHETIC data only.

Every value here is invented (SYNTHVERIFY / synth.verify@example.invalid ...).
Real owner emails/cards never appear. OCR is faked (no PaddleOCR). OTP
delivery uses Django's locmem email backend, so codes are read from
``mail.outbox`` and never appear in logs.
"""

import io
import re
from datetime import timedelta

import pytest
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from PIL import Image

from accounts.models import User
from otp.models import OtpChallenge, OtpPurpose, OtpTargetState
from registration.models import (
    RegistrationIdentityExtractionJob,
    RegistrationSession,
)

START = "/api/v1/auth/register/email/start/"
RESEND = "/api/v1/auth/register/email/resend/"
VERIFY = "/api/v1/auth/register/email/verify/"
STATUS = "/api/v1/auth/register/email/status/"
EXTRACT = "/api/v1/auth/register/identity/extract/"
REGISTER = "/api/v1/auth/register/"

pytestmark = pytest.mark.django_db


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
            "name": {
                "value": "SYNTHNAME",
                "confidence": 0.9,
                "source": "FRONT_PRINTED",
            },
            "mother_name": {
                "value": "SYNTHMOTHER",
                "confidence": 0.9,
                "source": "FRONT_PRINTED",
            },
            "national_card_number": {
                "value": "999999999999",
                "confidence": 0.95,
                "source": "FRONT_PRINTED",
            },
            "document_number": {
                "value": "H12345678",
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
            "issue_date": {
                "value": "2024-02-03",
                "confidence": 0.9,
                "source": "BACK_PRINTED",
            },
            "expiry_date": {
                "value": "2034-02-02",
                "confidence": 0.9,
                "source": "BACK_PRINTED",
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
        "document_number": "H12345678",
        "national_card_number": "999999999999",
        "family_number": "TESTFAMILY123456",
        "unique_card_body_number": "H12345678",
        "issue_date": "2024-02-03",
        "expiry_date": "2034-02-02",
        "name": "SYNTHNAME",
        "father_name": "SYNTHFATHER",
        "grandfather_name": "SYNTHGRANDFATHER",
        "mother_name": "SYNTHMOTHER",
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
    settings.REGISTRATION_SESSION_TTL_SECONDS = 24 * 60 * 60
    settings.OTP_RESEND_COOLDOWN_SECONDS = 60
    settings.OTP_TTL_MINUTES = 10


def start_session(api_client, email="synth.verify@example.invalid", **overrides):
    mail.outbox.clear()
    payload = {"email": email}
    payload.update(overrides)
    resp = api_client.post(START, payload, format="json")
    return resp


def otp_code():
    body = mail.outbox[-1].body
    match = re.search(r"\n\n(\d{6})\n\n", body)
    assert match, "OTP code not found in delivered email body"
    return match.group(1)


def allow_resend(email="synth.verify@example.invalid"):
    state = (
        OtpTargetState.objects.filter(
            purpose=OtpPurpose.EMAIL_VERIFICATION
        )
        .order_by("-created_at")
        .first()
    )
    state.last_issued_at = timezone.now() - timedelta(seconds=61)
    state.save(update_fields=("last_issued_at", "updated_at"))


def verify_code(api_client, token, code):
    return api_client.post(
        VERIFY, {"session_token": token, "code": code}, format="json"
    )


# --------------------------------------------------------------------------- #
# START / STATUS / ENUMERATION
# --------------------------------------------------------------------------- #


def test_start_creates_session_and_sends_otp(api_client):
    resp = start_session(api_client)
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert len(data["session_token"]) >= 32
    assert data["masked_email"] == "s***y@example.invalid"
    assert data["status"] == "PENDING_EMAIL_VERIFICATION"
    assert data["resend_at"] is None
    assert len(mail.outbox) == 1
    assert re.fullmatch(r"\d{6}", otp_code())
    session = RegistrationSession.objects.get(
        capability_digest=__import__("hashlib").sha256(
            data["session_token"].encode()
        ).hexdigest()
    )
    assert session.email == "synth.verify@example.invalid"
    assert session.status == RegistrationSession.Status.PENDING_EMAIL_VERIFICATION
    # Password/verified are NEVER client-settable: no such fields on start.
    assert "password" not in data
    assert "verified" not in data


def test_start_rejects_forged_verified_fields(api_client):
    resp = api_client.post(
        START,
        {
            "email": "synth.forge@example.invalid",
            "verified": True,
            "email_verified": True,
            "email_verified_at": "2026-01-01T00:00:00Z",
        },
        format="json",
    )
    assert resp.status_code == 400
    assert not RegistrationSession.objects.exists()


def test_start_no_email_enumeration_for_existing_account(api_client):
    User.objects.create_user(
        email="existing@example.invalid",
        password="StrongPass123!",
        email_verified=False,
    )
    resp = start_session(api_client, email="existing@example.invalid")
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["masked_email"] == "e***g@example.invalid"
    # Generic response — same shape as a brand-new address.
    fresh = start_session(api_client, email="brand.new@example.invalid")
    assert fresh.status_code == 201
    assert fresh.json()["data"]["masked_email"] == "b***w@example.invalid"


def test_status_resume_with_correct_token(api_client):
    data = start_session(api_client).json()["data"]
    resp = api_client.get(
        STATUS, headers={"X-Registration-Session-Token": data["session_token"]}
    )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["masked_email"] == "s***y@example.invalid"
    assert body["email_verified"] is False
    assert body["status"] == "PENDING_EMAIL_VERIFICATION"
    assert body["expires_at"]


def test_status_wrong_or_missing_token_404_no_leak(api_client):
    start_session(api_client)
    assert (
        api_client.get(STATUS).status_code == 404
    )
    assert (
        api_client.get(
            STATUS, headers={"X-Registration-Session-Token": "wrong"}
        ).status_code
        == 404
    )


# --------------------------------------------------------------------------- #
# VERIFY / REPLAY / TARGET ISOLATION
# --------------------------------------------------------------------------- #


def test_verify_success_marks_session_verified(api_client):
    data = start_session(api_client).json()["data"]
    code = otp_code()
    resp = verify_code(api_client, data["session_token"], code)
    assert resp.status_code == 200, resp.content
    body = resp.json()["data"]
    assert body["email_verified"] is True
    assert body["status"] == "EMAIL_VERIFIED"
    assert body["email_verified_at"]
    session = RegistrationSession.objects.get(uuid=body["session_id"])
    assert session.status == RegistrationSession.Status.EMAIL_VERIFIED
    assert session.email_verified_at is not None


def test_verify_wrong_code_denied(api_client):
    data = start_session(api_client).json()["data"]
    resp = verify_code(api_client, data["session_token"], "000000")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "validation_error"


def test_verify_replay_denied(api_client):
    data = start_session(api_client).json()["data"]
    code = otp_code()
    assert verify_code(api_client, data["session_token"], code).status_code == 200
    # Same code again: challenge consumed -> denied.
    resp = verify_code(api_client, data["session_token"], code)
    assert resp.status_code == 400


def test_verify_expired_otp_denied(api_client):
    data = start_session(api_client).json()["data"]
    code = otp_code()
    challenge = OtpChallenge.objects.order_by("-created_at").first()
    challenge.expires_at = timezone.now() - timedelta(seconds=1)
    challenge.save(update_fields=("expires_at", "updated_at"))
    resp = verify_code(api_client, data["session_token"], code)
    assert resp.status_code == 400


def test_verify_wrong_email_target_isolation(api_client):
    """An OTP issued for session A's email cannot verify session B."""
    a = start_session(api_client, email="synth.a@example.invalid").json()["data"]
    code_a = otp_code()
    b = start_session(api_client, email="synth.b@example.invalid").json()["data"]
    assert a["session_token"] != b["session_token"]
    # Session B has no challenge for email A -> the code must fail.
    resp = verify_code(api_client, b["session_token"], code_a)
    assert resp.status_code == 400
    # And the correct code for B still works.
    assert (
        verify_code(api_client, b["session_token"], otp_code()).status_code == 200
    )


def test_verify_rejects_forged_verified_flag(api_client):
    data = start_session(api_client).json()["data"]
    resp = api_client.post(
        VERIFY,
        {
            "session_token": data["session_token"],
            "code": otp_code(),
            "email_verified": True,
        },
        format="json",
    )
    assert resp.status_code == 400  # unknown field rejected
    session = RegistrationSession.objects.get(uuid=data["session_id"])
    assert session.status != RegistrationSession.Status.EMAIL_VERIFIED


# --------------------------------------------------------------------------- #
# RESEND / COOLDOWN / INVALIDATION
# --------------------------------------------------------------------------- #


def test_resend_cooldown_returns_429(api_client):
    data = start_session(api_client).json()["data"]
    resp = api_client.post(
        RESEND, {"session_token": data["session_token"]}, format="json"
    )
    assert resp.status_code == 429
    assert "retry_after" in resp.json()["error"]["details"]


def test_resend_invalidates_old_code(api_client):
    data = start_session(api_client).json()["data"]
    old_code = otp_code()
    allow_resend()
    resp = api_client.post(
        RESEND, {"session_token": data["session_token"]}, format="json"
    )
    assert resp.status_code == 200
    new_code = otp_code()
    assert new_code != old_code
    # Old code denied; new code accepted.
    assert verify_code(api_client, data["session_token"], old_code).status_code == 400
    assert (
        verify_code(api_client, data["session_token"], new_code).status_code == 200
    )


def test_resend_after_verified_returns_409(api_client):
    data = start_session(api_client).json()["data"]
    assert verify_code(api_client, data["session_token"], otp_code()).status_code == 200
    resp = api_client.post(
        RESEND, {"session_token": data["session_token"]}, format="json"
    )
    assert resp.status_code == 409


def test_resend_wrong_token_404(api_client):
    resp = api_client.post(RESEND, {"session_token": "wrong"}, format="json")
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# OCR GATING + FINALIZE
# --------------------------------------------------------------------------- #


def test_unverified_session_cannot_start_identity_ocr(api_client):
    data = start_session(api_client).json()["data"]
    resp = api_client.post(
        EXTRACT,
        {
            "document_type": "UNIFIED_NATIONAL_CARD",
            "front_image": synthetic_png(),
            "back_image": synthetic_png(),
        },
        headers={"X-Registration-Session-Token": data["session_token"]},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "registration_email_not_verified"
    assert not RegistrationIdentityExtractionJob.objects.exists()


def test_missing_session_token_cannot_start_identity_ocr(api_client):
    resp = api_client.post(
        EXTRACT,
        {
            "document_type": "UNIFIED_NATIONAL_CARD",
            "front_image": synthetic_png(),
            "back_image": synthetic_png(),
        },
    )
    assert resp.status_code == 404


def test_verified_registration_proceeds_end_to_end(api_client):
    data = start_session(api_client).json()["data"]
    assert verify_code(api_client, data["session_token"], otp_code()).status_code == 200

    resp = api_client.post(
        EXTRACT,
        {
            "document_type": "UNIFIED_NATIONAL_CARD",
            "front_image": synthetic_png(),
            "back_image": synthetic_png(),
        },
        headers={"X-Registration-Session-Token": data["session_token"]},
    )
    assert resp.status_code == 202, resp.content
    job = resp.json()["data"]
    job_row = RegistrationIdentityExtractionJob.objects.get(uuid=job["job_id"])
    assert job_row.status == RegistrationIdentityExtractionJob.Status.SUCCESS
    assert job_row.session.status == RegistrationSession.Status.EMAIL_VERIFIED

    reg = api_client.post(
        REGISTER,
        {
            "email": "synth.verify@example.invalid",
            "password": "StrongPass123!",
            "governorate": "BAGHDAD",
            "registration_session": data["session_token"],
            "registration_identity": confirmed_identity(
                job["job_id"], job["job_token"]
            ),
        },
        format="json",
    )
    assert reg.status_code == 201, reg.content
    user = User.objects.get(email="synth.verify@example.invalid")
    assert user.email_verified is True
    assert user.email_verified_at is not None
    session = RegistrationSession.objects.get(uuid=data["session_id"])
    assert session.status == RegistrationSession.Status.FINALIZED
    assert session.finalized_at is not None


def test_register_without_session_denied(api_client):
    data = start_session(api_client).json()["data"]
    assert verify_code(api_client, data["session_token"], otp_code()).status_code == 200
    job = api_client.post(
        EXTRACT,
        {
            "document_type": "UNIFIED_NATIONAL_CARD",
            "front_image": synthetic_png(),
            "back_image": synthetic_png(),
        },
        headers={"X-Registration-Session-Token": data["session_token"]},
    ).json()["data"]
    resp = api_client.post(
        REGISTER,
        {
            "email": "synth.verify@example.invalid",
            "password": "StrongPass123!",
            "governorate": "BAGHDAD",
            "registration_identity": confirmed_identity(
                job["job_id"], job["job_token"]
            ),
        },
        format="json",
    )
    assert resp.status_code == 400
    assert "registration_session" in resp.json()["error"]["details"]
    assert not User.objects.filter(email="synth.verify@example.invalid").exists()


def test_register_forged_verified_flag_denied(api_client):
    data = start_session(api_client).json()["data"]
    assert verify_code(api_client, data["session_token"], otp_code()).status_code == 200
    job = api_client.post(
        EXTRACT,
        {
            "document_type": "UNIFIED_NATIONAL_CARD",
            "front_image": synthetic_png(),
            "back_image": synthetic_png(),
        },
        headers={"X-Registration-Session-Token": data["session_token"]},
    ).json()["data"]
    resp = api_client.post(
        REGISTER,
        {
            "email": "synth.verify@example.invalid",
            "password": "StrongPass123!",
            "governorate": "BAGHDAD",
            "registration_session": data["session_token"],
            "registration_identity": confirmed_identity(
                job["job_id"], job["job_token"]
            ),
            "email_verified": True,
            "verified": True,
        },
        format="json",
    )
    assert resp.status_code == 400  # unknown fields rejected
    assert not User.objects.filter(email="synth.verify@example.invalid").exists()


def test_finalize_with_unverified_session_denied(api_client):
    """Defense in depth: even a job created for an unverified session cannot
    finalize (session is checked inside the final transaction)."""
    data = start_session(api_client).json()["data"]
    session = RegistrationSession.objects.get(uuid=data["session_id"])
    job = RegistrationIdentityExtractionJob.objects.create(
        capability_digest="a" * 64,
        session=session,
        status=RegistrationIdentityExtractionJob.Status.SUCCESS,
        expires_at=timezone.now() + timedelta(minutes=30),
    )
    token = "x" * 64
    job.capability_digest = __import__("hashlib").sha256(token.encode()).hexdigest()
    job.save(update_fields=("capability_digest",))
    resp = api_client.post(
        REGISTER,
        {
            "email": "synth.verify@example.invalid",
            "password": "StrongPass123!",
            "governorate": "BAGHDAD",
            "registration_session": data["session_token"],
            "registration_identity": confirmed_identity(job.uuid, token),
        },
        format="json",
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "registration_email_not_verified"
    assert not User.objects.filter(email="synth.verify@example.invalid").exists()


def test_existing_user_grandfathered_not_locked_out(api_client):
    """Existing accounts are unaffected: gate only guards pre-registration."""
    user = User.objects.create_user(
        email="grandfathered@example.invalid",
        password="StrongPass123!",
        email_verified=False,
        status=User.Status.ACTIVE,
    )
    user.refresh_from_db()
    assert user.email_verified is False
    assert user.email_verified_at is None
    # Login still works.
    resp = api_client.post(
        "/api/v1/auth/login/",
        {
            "email": "grandfathered@example.invalid",
            "password": "StrongPass123!",
        },
        format="json",
    )
    assert resp.status_code == 200
