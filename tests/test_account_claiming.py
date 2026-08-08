import hashlib
import io
from datetime import date, timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from rest_framework_simplejwt.tokens import RefreshToken

from claims.models import AccountActivation, PatientAccountClaim
from identities.models import IdentityDocument
from identities.services import persist_identity_upload
from patients.models import PatientProfile
from tests.factories import UserFactory

pytestmark = pytest.mark.django_db
PUBLIC = "/api/v1/account-claims/"
VERIFY = "/api/v1/verification/account-claims/"
ACTIVATE = "/api/v1/auth/activate-claimed-account/"


@pytest.fixture(autouse=True)
def private_identity_storage(settings, tmp_path):
    settings.IDENTITY_FILE_ROOT = tmp_path / "identity"
    settings.IDENTITY_FILE_MAX_BYTES = 1024 * 1024


def image_upload(name="card.png"):
    stream = io.BytesIO()
    Image.new("RGB", (8, 8), color=(20, 30, 40)).save(stream, format="PNG")
    return SimpleUploadedFile(name, stream.getvalue(), content_type="image/png")


def verified_adult(*, digital_id="12345678901234567", owner=None):
    profile = PatientProfile.objects.create(
        user=owner,
        digital_id=digital_id,
        full_name="Layla Hassan",
        date_of_birth=date(1990, 1, 2),
        sex=PatientProfile.Sex.FEMALE,
        nationality="IQ",
        identity_status=PatientProfile.IdentityStatus.VERIFIED,
    )
    front = persist_identity_upload(image_upload("existing-front.png"))
    back = persist_identity_upload(image_upload("existing-back.png"))
    IdentityDocument.objects.create(
        patient=profile,
        document_type=IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD,
        document_number="CARD-001",
        issuing_country="IQ",
        front_image=front,
        back_image=back,
        status=IdentityDocument.LifecycleStatus.CURRENT,
        verification_status=IdentityDocument.VerificationStatus.VERIFIED,
    )
    return profile


def payload(**overrides):
    data = {
        "digital_id": "12345678901234567",
        "email": "Claimant@Example.COM",
        "phone": "+9647701234567",
        "full_name": "Layla Hassan",
        "date_of_birth": "1990-01-02",
        "identity_document_type": "UNIFIED_NATIONAL_CARD",
        "identity_document_number": "CARD-001",
        "front_image": image_upload("front.png"),
        "back_image": image_upload("back.png"),
    }
    data.update(overrides)
    return data


def auth(client, user):
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}"
    )


def submit(client, data=None):
    return client.post(PUBLIC, data or payload(), format="multipart")


def test_eligible_public_submission_is_persisted_with_private_evidence(api_client):
    profile = verified_adult()
    response = submit(api_client)
    assert response.status_code == 202
    assert set(response.json()) == {"data"}
    assert set(response.json()["data"]) == {"claim_id", "status"}
    claim = PatientAccountClaim.objects.get()
    assert str(claim.uuid) == response.json()["data"]["claim_id"]
    assert claim.patient == profile
    assert claim.requested_email == "claimant@example.com"
    assert claim.name_comparison == "MATCH"
    assert claim.date_of_birth_comparison == "MATCH"
    assert claim.document_number_comparison == "MATCH"
    with pytest.raises(ValueError):
        claim.identity_evidence.get().front_image.file.storage.url("private")


@pytest.mark.parametrize(
    "condition",
    ["unknown", "minor", "owned", "email", "duplicate"],
)
def test_ineligible_submissions_return_indistinguishable_decoy_receipts(
    api_client, condition
):
    profile = None
    if condition != "unknown":
        owner = UserFactory(status="ACTIVE") if condition == "owned" else None
        profile = verified_adult(owner=owner)
    if condition == "minor":
        PatientProfile.objects.filter(pk=profile.pk).update(
            date_of_birth=date.today() - timedelta(days=17 * 365)
        )
    if condition == "email":
        UserFactory(email="claimant@example.com")
    if condition == "duplicate":
        PatientAccountClaim.objects.create(
            patient=profile,
            requested_email="first@example.com",
            requested_phone="+9647701234567",
            submitted_name=profile.full_name,
            submitted_date_of_birth=profile.date_of_birth,
        )
    response = submit(api_client)
    assert response.status_code == 202
    assert response.json()["data"]["status"] == "PENDING"
    assert PatientAccountClaim.objects.count() == (1 if condition == "duplicate" else 0)


@pytest.mark.parametrize(
    "change",
    [
        {"identity_document_type": "PASSPORT"},
        {"front_image": SimpleUploadedFile("x.txt", b"bad", content_type="text/plain")},
        {"status": "APPROVED"},
        {"email": "malformed"},
        {"phone": "123"},
        {"digital_id": "not-a-digital-id"},
    ],
)
def test_public_submission_rejects_invalid_or_internal_fields(api_client, change):
    verified_adult()
    response = submit(api_client, payload(**change))
    assert response.status_code == 400
    assert set(response.json()) == {"error"}


def test_mismatches_are_review_signals_and_never_overwrite_identity(api_client):
    profile = verified_adult()
    response = submit(
        api_client,
        payload(
            full_name="Different Name",
            date_of_birth="1985-03-04",
            identity_document_number="DIFFERENT-CARD",
        ),
    )
    assert response.status_code == 202
    claim = PatientAccountClaim.objects.get()
    assert (
        claim.name_comparison,
        claim.date_of_birth_comparison,
        claim.document_number_comparison,
    ) == ("MISMATCH", "MISMATCH", "MISMATCH")
    profile.refresh_from_db()
    assert profile.full_name == "Layla Hassan"
    assert profile.date_of_birth == date(1990, 1, 2)


def test_exact_role_can_review_and_approve_then_activation_enables_login(api_client):
    profile = verified_adult()
    submit(api_client)
    claim = PatientAccountClaim.objects.get()
    agent = UserFactory(
        role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE", email="agent@example.com"
    )
    auth(api_client, agent)
    queue = api_client.get(VERIFY)
    assert queue.status_code == 200
    assert queue.json()["data"]["count"] == 1
    approval = api_client.post(f"{VERIFY}{claim.uuid}/approve/", {}, format="json")
    assert approval.status_code == 200
    token = approval.json()["data"]["activation_token"]
    claim.refresh_from_db()
    profile.refresh_from_db()
    assert claim.status == "APPROVED"
    assert profile.user.status == "PENDING_ACTIVATION"
    assert not profile.user.has_usable_password()
    assert (
        AccountActivation.objects.get().token_hash
        == hashlib.sha256(token.encode()).hexdigest()
    )
    assert token not in AccountActivation.objects.get().token_hash

    api_client.credentials()
    denied = api_client.post(
        "/api/v1/auth/login/",
        {"email": "claimant@example.com", "password": "StrongPass456!"},
        format="json",
    )
    assert denied.status_code == 401
    activated = api_client.post(
        ACTIVATE,
        {"token": token, "new_password": "StrongPass456!"},
        format="json",
    )
    assert activated.status_code == 200
    assert activated.json()["data"]["message"] == "Account activated."
    assert (
        api_client.post(
            ACTIVATE, {"token": token, "new_password": "OtherPass456!"}, format="json"
        ).status_code
        == 400
    )
    assert (
        api_client.post(
            "/api/v1/auth/login/",
            {"email": "claimant@example.com", "password": "StrongPass456!"},
            format="json",
        ).status_code
        == 200
    )


@pytest.mark.parametrize("role", ["PATIENT", "ADMIN"])
def test_non_agent_cannot_review_or_approve(api_client, role):
    verified_adult()
    submit(api_client)
    claim = PatientAccountClaim.objects.get()
    auth(api_client, UserFactory(status="ACTIVE", role=role))
    assert api_client.get(VERIFY).status_code == 403
    assert (
        api_client.post(f"{VERIFY}{claim.uuid}/approve/", {}, format="json").status_code
        == 403
    )


def test_agent_can_request_more_information_and_reject(api_client):
    profile = verified_adult()
    submit(api_client)
    claim = PatientAccountClaim.objects.get()
    agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")
    auth(api_client, agent)
    more = api_client.post(
        f"{VERIFY}{claim.uuid}/request-more-information/",
        {"reason": "Provide a clearer image."},
        format="json",
    )
    assert more.status_code == 200
    claim.refresh_from_db()
    assert claim.status == "MORE_INFORMATION_REQUIRED"
    rejected = api_client.post(
        f"{VERIFY}{claim.uuid}/reject/",
        {"reason": "Identity evidence could not be verified."},
        format="json",
    )
    assert rejected.status_code == 200
    claim.refresh_from_db()
    assert claim.status == "REJECTED"
    assert claim.reviewed_by == agent
    assert claim.reviewed_at is not None
    assert (
        api_client.post(f"{VERIFY}{claim.uuid}/approve/", {}, format="json").status_code
        == 409
    )
    assert profile.user_id is None
