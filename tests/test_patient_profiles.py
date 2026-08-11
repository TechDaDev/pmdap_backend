import re
import uuid
from datetime import date, timedelta
from io import BytesIO
from unittest.mock import Mock, patch

import pytest
from django.apps import apps
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from PIL import Image
from rest_framework_simplejwt.tokens import RefreshToken

from tests.factories import UserFactory

REGISTER = "/api/v1/auth/register/"
PATIENT_ME = "/api/v1/patients/me/"
PASSWORD = "Correct-Horse-Battery-42!"
PROFILE_INPUT = {
    "full_name": "Layla Hassan",
    "date_of_birth": "1992-02-29",
    "sex": "FEMALE",
    "nationality": "IQ",
    "blood_group": "A+",
}
PROFILE_FIELDS = {
    "uuid",
    "digital_id",
    "full_name",
    "date_of_birth",
    "age",
    "is_minor",
    "sex",
    "nationality",
    "blood_group",
    "identity_status",
    "avatar_url",
    "created_at",
    "updated_at",
}


def patient_model():
    return apps.get_model("patients", "PatientProfile")


def assert_error(response, status_code, code=None):
    assert response.status_code == status_code
    assert set(response.json()) == {"error"}
    error = response.json()["error"]
    assert set(error) == {"code", "message", "details"}
    if code:
        assert error["code"] == code


def access_for(user):
    return str(RefreshToken.for_user(user).access_token)


def auth(api_client, user):
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_for(user)}")


def registration_payload(**patient_overrides):
    patient = {**PROFILE_INPUT, **patient_overrides}
    return {
        "email": "layla@example.com",
        "phone": "+9647700000000",
        "password": PASSWORD,
        "patient": patient,
    }


@pytest.mark.django_db
def test_patient_registration_creates_exactly_one_profile(api_client):
    response = api_client.post(REGISTER, registration_payload(), format="json")

    assert response.status_code == 201
    profile = patient_model().objects.get()
    user = apps.get_model("accounts", "User").objects.get()
    assert profile.user == user
    assert user.patient_profile == profile
    assert re.fullmatch(
        r"PT-[2-9A-HJ-NP-Z]{4}(?:-[2-9A-HJ-NP-Z]{4}){2}", profile.digital_id
    )
    assert profile.identity_status == profile.IdentityStatus.UNVERIFIED


@pytest.mark.django_db(transaction=True)
def test_registration_and_profile_creation_are_atomic(api_client):
    with patch(
        "accounts.services.create_patient_profile",
        side_effect=RuntimeError("profile creation failed"),
    ):
        with pytest.raises(RuntimeError, match="profile creation failed"):
            api_client.post(REGISTER, registration_payload(), format="json")

    assert not apps.get_model("accounts", "User").objects.exists()
    assert not patient_model().objects.exists()


@pytest.mark.django_db(transaction=True)
def test_profile_integrity_failure_is_not_mislabeled_as_duplicate_email(api_client):
    with patch(
        "accounts.services.create_patient_profile",
        side_effect=IntegrityError("profile database failure"),
    ):
        with pytest.raises(IntegrityError, match="profile database failure"):
            api_client.post(REGISTER, registration_payload(), format="json")

    assert not apps.get_model("accounts", "User").objects.exists()


@pytest.mark.django_db
def test_registration_rejects_client_supplied_digital_id(api_client):
    response = api_client.post(
        REGISTER,
        registration_payload(digital_id="PT-AAAA-BBBB-CCCC"),
        format="json",
    )

    assert_error(response, 400, "validation_error")
    assert "digital_id" in response.json()["error"]["details"]["patient"]


@pytest.mark.django_db
def test_duplicate_direct_profile_for_user_is_impossible():
    from patients.services import create_patient_profile

    user = UserFactory(status="ACTIVE")
    create_patient_profile(user=user, **PROFILE_INPUT)

    with pytest.raises(IntegrityError):
        patient_model().objects.create(
            user=user,
            digital_id="PT-AAAA-BBBB-CCCC",
            **PROFILE_INPUT,
        )


@pytest.mark.django_db
def test_non_patient_cannot_complete_patient_profile(api_client):
    user = UserFactory(role="ADMIN", status="ACTIVE")
    auth(api_client, user)

    response = api_client.post(PATIENT_ME, PROFILE_INPUT, format="json")

    assert_error(response, 403, "patient_role_required")
    assert not patient_model().objects.exists()


@pytest.mark.django_db
def test_profile_service_rejects_non_patient_direct_owner():
    from patients.services import create_patient_profile

    user = UserFactory(role="ADMIN", status="ACTIVE")

    with pytest.raises(DjangoValidationError, match="PATIENT"):
        create_patient_profile(user=user, **PROFILE_INPUT)


@pytest.mark.django_db
def test_profile_service_rejects_minor_direct_owner():
    from patients.services import create_patient_profile

    user = UserFactory(status="ACTIVE")
    minor_dob = date.today().replace(year=date.today().year - 10)

    with pytest.raises(DjangoValidationError, match="adult"):
        create_patient_profile(
            user=user, **{**PROFILE_INPUT, "date_of_birth": minor_dob}
        )


@pytest.mark.django_db
def test_legacy_patient_completes_profile_once(api_client):
    user = UserFactory(status="ACTIVE")
    auth(api_client, user)

    first = api_client.post(PATIENT_ME, PROFILE_INPUT, format="json")
    second = api_client.post(PATIENT_ME, PROFILE_INPUT, format="json")

    assert first.status_code == 201
    assert set(first.json()["data"]) == PROFILE_FIELDS
    assert_error(second, 409, "patient_profile_exists")
    assert patient_model().objects.filter(user=user).count() == 1


@pytest.mark.django_db
def test_legacy_profile_creation_race_returns_conflict(api_client):
    user = UserFactory(status="ACTIVE")
    auth(api_client, user)

    def concurrent_create(*, user, **identity):
        patient_model().objects.create(
            user=user,
            digital_id="PT-AAAA-BBBB-CCCC",
            **identity,
        )
        raise IntegrityError("concurrent profile")

    with patch("patients.api.create_patient_profile", side_effect=concurrent_create):
        response = api_client.post(PATIENT_ME, PROFILE_INPUT, format="json")

    assert_error(response, 409, "patient_profile_exists")


@pytest.mark.django_db
def test_legacy_patient_without_profile_is_explicitly_not_found(api_client):
    user = UserFactory(status="ACTIVE")
    auth(api_client, user)

    response = api_client.get(PATIENT_ME)

    assert_error(response, 404, "patient_profile_not_found")


def test_digital_id_generator_format_uniqueness_and_no_pii():
    from patients.services import generate_digital_id

    values = {generate_digital_id() for _ in range(2000)}

    assert len(values) == 2000
    assert all(
        re.fullmatch(r"PT-[2-9A-HJ-NP-Z]{4}(?:-[2-9A-HJ-NP-Z]{4}){2}", value)
        for value in values
    )
    joined = " ".join(values)
    for pii in ["Layla", "1992", "example.com", "9647700000000", "IQ"]:
        assert pii not in joined


@pytest.mark.django_db
def test_digital_id_collision_retries():
    from patients.services import create_patient_profile

    first = UserFactory(status="ACTIVE")
    second = UserFactory(status="ACTIVE")
    patient_model().objects.create(
        user=first,
        digital_id="PT-AAAA-BBBB-CCCC",
        **PROFILE_INPUT,
    )

    with patch(
        "patients.services.generate_digital_id",
        side_effect=["PT-AAAA-BBBB-CCCC", "PT-DDDD-EEEE-FFFF"],
    ) as generator:
        profile = create_patient_profile(user=second, **PROFILE_INPUT)

    assert profile.digital_id == "PT-DDDD-EEEE-FFFF"
    assert generator.call_count == 2


@pytest.mark.django_db
def test_digital_id_database_race_retries():
    from patients.models import PatientProfile
    from patients.services import create_patient_profile

    user = UserFactory(status="ACTIVE")
    original_save = PatientProfile.save
    save_calls = 0

    def race_once(instance, *args, **kwargs):
        nonlocal save_calls
        save_calls += 1
        if save_calls == 1:
            raise IntegrityError("simulated Digital ID race")
        return original_save(instance, *args, **kwargs)

    existence = [
        Mock(exists=Mock(return_value=False)),
        Mock(exists=Mock(return_value=True)),
        Mock(exists=Mock(return_value=False)),
    ]
    with (
        patch(
            "patients.services.generate_digital_id",
            side_effect=["PT-AAAA-BBBB-CCCC", "PT-DDDD-EEEE-FFFF"],
        ),
        patch.object(PatientProfile.objects, "filter", side_effect=existence),
        patch.object(PatientProfile, "save", new=race_once),
    ):
        profile = create_patient_profile(user=user, **PROFILE_INPUT)

    assert profile.digital_id == "PT-DDDD-EEEE-FFFF"
    assert save_calls == 2


@pytest.mark.django_db
def test_digital_id_collision_limit_fails_closed():
    from patients.services import DigitalIDGenerationError, create_patient_profile

    user = UserFactory(status="ACTIVE")
    patient_model().objects.create(
        digital_id="PT-AAAA-BBBB-CCCC",
        **PROFILE_INPUT,
    )

    with (
        patch(
            "patients.services.generate_digital_id",
            return_value="PT-AAAA-BBBB-CCCC",
        ),
        pytest.raises(DigitalIDGenerationError),
    ):
        create_patient_profile(user=user, **PROFILE_INPUT)


@pytest.mark.django_db
def test_non_collision_integrity_error_is_not_hidden():
    from patients.models import PatientProfile
    from patients.services import create_patient_profile

    user = UserFactory(status="ACTIVE")
    with (
        patch.object(PatientProfile, "save", side_effect=IntegrityError("db failure")),
        pytest.raises(IntegrityError, match="db failure"),
    ):
        create_patient_profile(user=user, **PROFILE_INPUT)


@pytest.mark.django_db
@pytest.mark.parametrize("field", ["uuid", "digital_id"])
def test_public_identifiers_are_model_immutable(field):
    from patients.services import create_patient_profile

    profile = create_patient_profile(user=UserFactory(status="ACTIVE"), **PROFILE_INPUT)
    setattr(
        profile,
        field,
        uuid.uuid4() if field == "uuid" else "PT-ZZZZ-ZZZZ-ZZZZ",
    )

    with pytest.raises(DjangoValidationError, match="immutable"):
        profile.save()


@pytest.mark.django_db
def test_uuid_remains_distinct_from_digital_id():
    from patients.services import create_patient_profile

    profile = create_patient_profile(user=UserFactory(status="ACTIVE"), **PROFILE_INPUT)

    assert isinstance(profile.uuid, uuid.UUID)
    assert str(profile.uuid) != profile.digital_id
    assert str(profile) == profile.digital_id


@pytest.mark.django_db
def test_patient_retrieves_only_own_public_profile(api_client):
    from patients.services import create_patient_profile

    user = UserFactory(status="ACTIVE")
    profile = create_patient_profile(user=user, **PROFILE_INPUT)
    auth(api_client, user)

    response = api_client.get(PATIENT_ME)

    assert response.status_code == 200
    assert set(response.json()["data"]) == PROFILE_FIELDS
    assert response.json()["data"]["uuid"] == str(profile.uuid)
    leaked = {"user", "user_id", "password", "email", "phone"}
    assert leaked.isdisjoint(response.json()["data"])


@pytest.mark.django_db
def test_patient_me_requires_authentication(api_client):
    response = api_client.get(PATIENT_ME)

    assert_error(response, 401, "not_authenticated")


@pytest.mark.django_db
def test_no_uuid_or_digital_id_lookup_endpoint_exposes_profiles(api_client):
    from patients.services import create_patient_profile

    owner = UserFactory(status="ACTIVE")
    attacker = UserFactory(status="ACTIVE")
    profile = create_patient_profile(user=owner, **PROFILE_INPUT)
    auth(api_client, attacker)

    by_uuid = api_client.get(f"/api/v1/patients/{profile.uuid}/")
    by_digital_id = api_client.get(f"/api/v1/patients/{profile.digital_id}/")

    assert by_uuid.status_code == 404
    assert by_digital_id.status_code == 404
    assert profile.full_name.encode() not in by_uuid.content
    assert profile.full_name.encode() not in by_digital_id.content


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("full_name", "Layla Al-Hassan"),
        ("date_of_birth", "1993-06-01"),
        ("sex", "UNSPECIFIED"),
        ("nationality", "JO"),
        ("blood_group", "O-"),
    ],
)
def test_allowed_partial_profile_update(api_client, field, value):
    from patients.services import create_patient_profile

    user = UserFactory(status="ACTIVE")
    profile = create_patient_profile(user=user, **PROFILE_INPUT)
    auth(api_client, user)

    response = api_client.patch(PATIENT_ME, {field: value}, format="json")

    assert response.status_code == 200
    profile.refresh_from_db()
    expected = date.fromisoformat(value) if field == "date_of_birth" else value
    assert getattr(profile, field) == expected


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("uuid", "a86fb2c7-4e42-4a16-95d1-569f80f94f7d"),
        ("digital_id", "PT-ZZZZ-ZZZZ-ZZZZ"),
        ("identity_status", "VERIFIED"),
        ("user", "a86fb2c7-4e42-4a16-95d1-569f80f94f7d"),
        ("created_at", "2020-01-01T00:00:00Z"),
        ("updated_at", "2020-01-01T00:00:00Z"),
        ("age", 99),
        ("is_minor", False),
    ],
)
def test_profile_update_rejects_protected_fields(api_client, field, value):
    from patients.services import create_patient_profile

    user = UserFactory(status="ACTIVE")
    profile = create_patient_profile(user=user, **PROFILE_INPUT)
    original = {
        "uuid": profile.uuid,
        "digital_id": profile.digital_id,
        "identity_status": profile.identity_status,
        "user": profile.user,
    }
    auth(api_client, user)

    response = api_client.patch(PATIENT_ME, {field: value}, format="json")

    assert_error(response, 400, "validation_error")
    assert field in response.json()["error"]["details"]
    profile.refresh_from_db()
    for key, expected in original.items():
        assert getattr(profile, key) == expected


@pytest.mark.django_db
def test_profile_update_rejects_nested_mass_assignment(api_client):
    from patients.services import create_patient_profile

    user = UserFactory(status="ACTIVE")
    create_patient_profile(user=user, **PROFILE_INPUT)
    auth(api_client, user)

    response = api_client.patch(
        PATIENT_ME,
        {"identity": {"identity_status": "VERIFIED"}},
        format="json",
    )

    assert_error(response, 400, "validation_error")
    assert "identity" in response.json()["error"]["details"]


@pytest.mark.django_db
def test_verified_identity_fields_are_locked(api_client):
    from patients.services import create_patient_profile

    user = UserFactory(status="ACTIVE")
    profile = create_patient_profile(user=user, **PROFILE_INPUT)
    patient_model().objects.filter(pk=profile.pk).update(identity_status="VERIFIED")
    auth(api_client, user)

    response = api_client.patch(
        PATIENT_ME, {"full_name": "Changed Name"}, format="json"
    )

    assert_error(response, 400, "validation_error")
    assert "full_name" in response.json()["error"]["details"]


@pytest.mark.django_db
def test_verified_patient_can_update_non_identity_blood_group(api_client):
    from patients.services import create_patient_profile

    user = UserFactory(status="ACTIVE")
    profile = create_patient_profile(user=user, **PROFILE_INPUT)
    patient_model().objects.filter(pk=profile.pk).update(identity_status="VERIFIED")
    auth(api_client, user)

    response = api_client.patch(PATIENT_ME, {"blood_group": "O+"}, format="json")

    assert response.status_code == 200
    profile.refresh_from_db()
    assert profile.blood_group == "O+"


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["put", "delete"])
def test_patient_me_unsupported_methods_return_405(api_client, method):
    user = UserFactory(status="ACTIVE")
    auth(api_client, user)

    response = getattr(api_client, method)(PATIENT_ME, PROFILE_INPUT, format="json")

    assert_error(response, 405, "method_not_allowed")


@pytest.mark.django_db
def test_future_dob_rejected(api_client):
    response = api_client.post(
        REGISTER,
        registration_payload(date_of_birth=str(date.today() + timedelta(days=1))),
        format="json",
    )

    assert_error(response, 400, "validation_error")
    assert "date_of_birth" in response.json()["error"]["details"]["patient"]


@pytest.mark.django_db
def test_direct_account_profile_rejects_minor_dob(api_client):
    today = date.today()
    minor_dob = today.replace(year=today.year - 18) + timedelta(days=1)

    response = api_client.post(
        REGISTER,
        registration_payload(date_of_birth=str(minor_dob)),
        format="json",
    )

    assert_error(response, 400, "validation_error")
    assert "date_of_birth" in response.json()["error"]["details"]["patient"]


@pytest.mark.parametrize(
    ("dob", "today", "expected_age", "expected_minor"),
    [
        (date(2000, 6, 15), date(2026, 6, 15), 26, False),
        (date(2008, 6, 16), date(2026, 6, 15), 17, True),
        (date(2008, 6, 15), date(2026, 6, 15), 18, False),
        (date(2004, 2, 29), date(2025, 2, 28), 20, False),
        (date(2004, 2, 29), date(2025, 3, 1), 21, False),
    ],
)
def test_age_and_minor_boundaries(dob, today, expected_age, expected_minor):
    profile = patient_model()(date_of_birth=dob)

    assert profile.age_on(today) == expected_age
    with patch("patients.models.timezone.localdate", return_value=today):
        assert profile.age == expected_age
        assert profile.is_minor is expected_minor


def avatar_upload(name="avatar.png"):
    stream = BytesIO()
    Image.new("RGB", (8, 8), color=(70, 120, 180)).save(stream, format="PNG")
    return SimpleUploadedFile(name, stream.getvalue(), content_type="image/png")


def avatar_patient(api_client):
    from patients.services import create_patient_profile

    user = UserFactory(status="ACTIVE")
    create_patient_profile(user=user, **PROFILE_INPUT)
    auth(api_client, user)
    return user


@pytest.mark.django_db
def test_avatar_url_null_when_unset(api_client):
    avatar_patient(api_client)

    response = api_client.get(PATIENT_ME)

    assert response.status_code == 200
    assert response.json()["data"]["avatar_url"] is None


@pytest.mark.django_db
def test_avatar_upload_via_patch(api_client):
    avatar_patient(api_client)

    response = api_client.patch(
        PATIENT_ME, {"avatar": avatar_upload()}, format="multipart"
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["avatar_url"] == f"{PATIENT_ME}avatar/"
    assert data["full_name"] == PROFILE_INPUT["full_name"]
    profile = patient_model().objects.get()
    assert profile.avatar
    assert profile.avatar.name


@pytest.mark.django_db
def test_avatar_serve_returns_private_bytes(api_client):
    avatar_patient(api_client)
    api_client.patch(PATIENT_ME, {"avatar": avatar_upload()}, format="multipart")

    response = api_client.get(f"{PATIENT_ME}avatar/")

    assert response.status_code == 200
    assert response["Content-Type"].startswith("image/")
    assert b"".join(response.streaming_content)[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.django_db
def test_avatar_serve_without_avatar_returns_404(api_client):
    avatar_patient(api_client)

    assert api_client.get(f"{PATIENT_ME}avatar/").status_code == 404


@pytest.mark.django_db
def test_avatar_requires_authentication(api_client):
    assert api_client.get(f"{PATIENT_ME}avatar/").status_code == 401


@pytest.mark.django_db
def test_malformed_dob_rejected(api_client):
    response = api_client.post(
        REGISTER,
        registration_payload(date_of_birth="31-12-1990"),
        format="json",
    )

    assert_error(response, 400, "validation_error")


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "value", "valid"),
    [
        ("sex", "MALE", True),
        ("sex", "INVALID", False),
        ("blood_group", "AB-", True),
        ("blood_group", "X+", False),
        ("nationality", "jo", True),
        ("nationality", "Iraq", False),
        ("nationality", "1Q", False),
    ],
)
def test_profile_enums_and_nationality_validation(api_client, field, value, valid):
    response = api_client.post(
        REGISTER, registration_payload(**{field: value}), format="json"
    )

    assert response.status_code == (201 if valid else 400)
    if valid and field == "nationality":
        assert patient_model().objects.get().nationality == "JO"
    if not valid:
        assert field in response.json()["error"]["details"]["patient"]


@pytest.mark.django_db
def test_patient_openapi_matches_runtime_contract(api_client):
    schema = api_client.get("/api/v1/schema/", HTTP_ACCEPT="application/json").json()
    path = schema["paths"][PATIENT_ME]

    assert set(path) == {"get", "patch", "post"}
    for method in ("get", "patch", "post"):
        assert path[method]["security"]
        assert path[method]["responses"]
    for method in ("patch", "post"):
        assert path[method]["requestBody"]["content"]["application/json"]["schema"]

    register_schema = schema["paths"][REGISTER]["post"]["requestBody"]["content"]
    assert register_schema["application/json"]["schema"]
