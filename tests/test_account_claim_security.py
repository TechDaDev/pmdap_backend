from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from claims.models import (
    AccountActivation,
    PatientAccountClaim,
    PatientAccountClaimEvent,
)
from claims.services.review import approve_account_claim
from guardians.models import GuardianRelationship
from tests.factories import UserFactory
from tests.test_account_claiming import (
    ACTIVATE,
    PUBLIC,
    VERIFY,
    auth,
    payload,
    submit,
    verified_adult,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def private_identity_storage(settings, tmp_path):
    settings.IDENTITY_FILE_ROOT = tmp_path / "private-identity"
    settings.IDENTITY_FILE_MAX_BYTES = 1024 * 1024


def agent():
    return UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")


def submitted_claim(client):
    profile = verified_adult()
    assert submit(client).status_code == 202
    return profile, PatientAccountClaim.objects.get()


def approved_claim(client):
    profile, claim = submitted_claim(client)
    reviewer = agent()
    result = approve_account_claim(claim=claim, agent=reviewer)
    return profile, claim, result


def test_submission_throttle_uses_consistent_error_envelope(api_client, settings):
    rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
    original = rates["account_claim_submit"]
    try:
        rates["account_claim_submit"] = "1/hour"
        first = submit(api_client)
        second = submit(api_client, payload(digital_id="99999999999999999"))
        assert first.status_code == 202
        assert second.status_code == 429
        assert set(second.json()) == {"error"}
        assert second.json()["error"]["code"] == "throttled"
    finally:
        rates["account_claim_submit"] = original


def test_public_contract_has_no_status_or_detail_resource(api_client):
    profile = verified_adult()
    response = submit(api_client)
    claim_id = response.json()["data"]["claim_id"]
    assert api_client.get(PUBLIC).status_code == 405
    assert api_client.get(f"{PUBLIC}{claim_id}/").status_code == 404
    assert profile.user_id is None


def test_optional_passport_is_separate_claim_evidence(api_client):
    verified_adult()
    response = submit(
        api_client,
        payload(
            passport_number="P1234567",
            passport_issuing_country="IQ",
            passport_issue_date="2024-01-01",
            passport_expiry_date="2034-01-01",
            passport_front_image=payload()["front_image"],
        ),
    )
    assert response.status_code == 202
    assert set(
        PatientAccountClaim.objects.get().identity_evidence.values_list(
            "document_type", flat=True
        )
    ) == {"UNIFIED_NATIONAL_CARD", "PASSPORT"}


def test_future_birth_date_is_structurally_rejected(api_client):
    verified_adult()
    response = submit(api_client, payload(date_of_birth="2999-01-01"))
    assert response.status_code == 400
    assert PatientAccountClaim.objects.count() == 0


def test_storage_is_cleaned_if_submission_transaction_fails(api_client, settings):
    verified_adult()
    before = {path for path in settings.IDENTITY_FILE_ROOT.rglob("*") if path.is_file()}
    with (
        patch(
            "claims.services.submission.PatientAccountClaimEvent.objects.create",
            side_effect=RuntimeError("injected"),
        ),
        pytest.raises(RuntimeError, match="injected"),
    ):
        submit(api_client)
    after = {path for path in settings.IDENTITY_FILE_ROOT.rglob("*") if path.is_file()}
    assert after == before
    assert PatientAccountClaim.objects.count() == 0


def test_agent_detail_and_evidence_are_exact_role_private(api_client):
    profile, claim = submitted_claim(api_client)
    relationship = GuardianRelationship.objects.create(
        guardian_user=UserFactory(status="ACTIVE"),
        minor_patient=profile,
        relationship=GuardianRelationship.Relationship.FATHER,
        verification_status=GuardianRelationship.VerificationStatus.VERIFIED,
        active=True,
    )
    evidence = claim.identity_evidence.get()
    reviewer = agent()
    auth(api_client, reviewer)
    detail = api_client.get(f"{VERIFY}{claim.uuid}/")
    api_client.credentials()
    assert (
        api_client.get(
            f"{VERIFY}{claim.uuid}/evidence/{evidence.uuid}/images/front/"
        ).status_code
        == 401
    )
    auth(api_client, reviewer)
    image = api_client.get(
        f"{VERIFY}{claim.uuid}/evidence/{evidence.uuid}/images/front/"
    )
    assert detail.status_code == 200
    data = detail.json()["data"]
    assert data["existing_identity"] == {
        "digital_id": profile.digital_id,
        "full_name": profile.full_name,
        "date_of_birth": str(profile.date_of_birth),
        "identity_status": "VERIFIED",
    }
    assert data["identity_history"][0]["document_number"] == "CARD-001"
    assert data["guardian_history"][0]["uuid"] == str(relationship.uuid)
    assert "medical_reports" not in str(data)
    assert image.status_code == 200
    assert image["Content-Disposition"].startswith("attachment;")
    auth(api_client, UserFactory(status="ACTIVE"))
    assert (
        api_client.get(
            f"{VERIFY}{claim.uuid}/evidence/{evidence.uuid}/images/front/"
        ).status_code
        == 403
    )


def test_approval_replay_is_stable_conflict_and_never_reissues_token(api_client):
    _, claim, _ = approved_claim(api_client)
    claim.refresh_from_db()
    reviewer = claim.reviewed_by
    auth(api_client, reviewer)
    response = api_client.post(f"{VERIFY}{claim.uuid}/approve/", {}, format="json")
    assert response.status_code == 409
    assert "activation_token" not in str(response.json())
    assert set(claim.events.values_list("event_type", flat=True)) >= {
        "CLAIM_SUBMITTED",
        "CLAIM_UNDER_REVIEW",
        "PATIENT_ACCOUNT_LINKED",
        "ACCOUNT_ACTIVATION_CREATED",
        "CLAIM_APPROVED",
    }


def test_approval_rolls_back_all_authoritative_changes_on_failure(api_client):
    profile, claim = submitted_claim(api_client)
    reviewer = agent()
    guardian = UserFactory(status="ACTIVE")
    relationship = GuardianRelationship.objects.create(
        guardian_user=guardian,
        minor_patient=profile,
        relationship=GuardianRelationship.Relationship.LEGAL_GUARDIAN,
        verification_status=GuardianRelationship.VerificationStatus.VERIFIED,
        active=True,
    )
    before_events = PatientAccountClaimEvent.objects.count()
    with (
        patch(
            "claims.services.review.AccountActivation.objects.create",
            side_effect=RuntimeError("injected"),
        ),
        pytest.raises(RuntimeError, match="injected"),
    ):
        approve_account_claim(claim=claim, agent=reviewer)
    profile.refresh_from_db()
    relationship.refresh_from_db()
    claim.refresh_from_db()
    assert profile.user_id is None
    assert relationship.active is True
    assert claim.status == PatientAccountClaim.Status.PENDING
    assert AccountActivation.objects.count() == 0
    assert PatientAccountClaimEvent.objects.count() == before_events


def test_approval_revalidates_ownership_and_verified_identity(api_client):
    profile, claim = submitted_claim(api_client)
    profile.user = UserFactory(status="ACTIVE")
    profile.save(update_fields=("user", "updated_at"))
    with pytest.raises(Exception) as captured:
        approve_account_claim(claim=claim, agent=agent())
    assert getattr(captured.value, "status_code", None) == 409


@pytest.mark.parametrize(
    "target",
    [
        "claims.services.review.User.objects.create_user",
        "claims.services.review.PatientProfile.save",
        "claims.services.review.AccountActivation.objects.create",
        "claims.services.review.PatientAccountClaimEvent.objects.create",
    ],
)
def test_each_critical_approval_failure_rolls_back(api_client, target):
    profile, claim = submitted_claim(api_client)
    reviewer = agent()
    users_before = UserFactory._meta.model.objects.count()
    with (
        patch(target, side_effect=RuntimeError("critical-stage")),
        pytest.raises(RuntimeError, match="critical-stage"),
    ):
        approve_account_claim(claim=claim, agent=reviewer)
    profile.refresh_from_db()
    claim.refresh_from_db()
    assert profile.user_id is None
    assert claim.status == "PENDING"
    assert AccountActivation.objects.count() == 0
    assert UserFactory._meta.model.objects.count() == users_before


def test_expired_activation_is_rejected_without_state_change(api_client):
    profile, _, result = approved_claim(api_client)
    AccountActivation.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    response = api_client.post(
        ACTIVATE,
        {"token": result.activation_token, "new_password": "StrongPass456!"},
        format="json",
    )
    assert response.status_code == 400
    profile.refresh_from_db()
    profile.user.refresh_from_db()
    assert profile.user.status == "PENDING_ACTIVATION"
    assert AccountActivation.objects.get().used_at is None


def test_weak_activation_password_is_rejected_without_consuming_token(api_client):
    profile, _, result = approved_claim(api_client)
    response = api_client.post(
        ACTIVATE,
        {"token": result.activation_token, "new_password": "password"},
        format="json",
    )
    assert response.status_code == 400
    profile.refresh_from_db()
    profile.user.refresh_from_db()
    assert profile.user.status == "PENDING_ACTIVATION"
    assert AccountActivation.objects.get().used_at is None


def test_activation_revalidates_approved_claim_linkage(api_client):
    profile, claim, result = approved_claim(api_client)
    PatientAccountClaim.objects.filter(pk=claim.pk).update(status="CANCELLED")
    response = api_client.post(
        ACTIVATE,
        {"token": result.activation_token, "new_password": "StrongPass456!"},
        format="json",
    )
    assert response.status_code == 400
    profile.refresh_from_db()
    assert profile.user.status == "PENDING_ACTIVATION"
    assert AccountActivation.objects.get().used_at is None


@pytest.mark.parametrize("token", ["x" * 20, "not-the-correct-activation-token"])
def test_wrong_activation_tokens_use_generic_failure(api_client, token):
    approved_claim(api_client)
    response = api_client.post(
        ACTIVATE,
        {"token": token, "new_password": "StrongPass456!"},
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_activation_token"


def test_activation_endpoint_is_independently_throttled(api_client, settings):
    approved_claim(api_client)
    rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
    original = rates["account_claim_activation"]
    try:
        rates["account_claim_activation"] = "1/hour"
        body = {"token": "x" * 20, "new_password": "StrongPass456!"}
        assert api_client.post(ACTIVATE, body, format="json").status_code == 400
        response = api_client.post(ACTIVATE, body, format="json")
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "throttled"
    finally:
        rates["account_claim_activation"] = original


def test_pending_activation_account_cannot_login_or_use_jwt(api_client):
    user = UserFactory(
        email="pending-activation@example.com", status="PENDING_ACTIVATION"
    )
    login = api_client.post(
        "/api/v1/auth/login/",
        {"email": user.email, "password": "ChangeMe123!"},
        format="json",
    )
    assert login.status_code == 401
    auth(api_client, user)
    me = api_client.get("/api/v1/auth/me/")
    assert me.status_code == 401
    assert me.json()["error"]["code"] == "account_unavailable"


def test_m5_openapi_contains_actual_endpoints_and_security(api_client):
    response = api_client.get("/api/v1/schema/", HTTP_ACCEPT="application/json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    expected = {
        PUBLIC,
        ACTIVATE,
        VERIFY,
        f"{VERIFY}{{claim_uuid}}/",
        f"{VERIFY}{{claim_uuid}}/approve/",
        f"{VERIFY}{{claim_uuid}}/reject/",
        f"{VERIFY}{{claim_uuid}}/request-more-information/",
        f"{VERIFY}{{claim_uuid}}/evidence/{{evidence_uuid}}/images/{{side}}/",
    }
    assert expected <= set(paths)
    assert paths[PUBLIC]["post"]["security"] == [{}]
    assert paths[ACTIVATE]["post"]["security"] == [{}]
    assert paths[VERIFY]["get"]["security"] != [{}]
    assert "202" in paths[PUBLIC]["post"]["responses"]
    assert "409" in paths[f"{VERIFY}{{claim_uuid}}/approve/"]["post"]["responses"]


@pytest.mark.parametrize(
    "url",
    [PUBLIC, ACTIVATE],
)
def test_public_endpoints_reject_unsupported_methods(api_client, url):
    assert api_client.put(url, {}, format="json").status_code == 405
