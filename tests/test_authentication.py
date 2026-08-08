from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from tests.factories import UserFactory

REGISTER = "/api/v1/auth/register/"
LOGIN = "/api/v1/auth/login/"
REFRESH = "/api/v1/auth/refresh/"
LOGOUT = "/api/v1/auth/logout/"
ME = "/api/v1/auth/me/"
PASSWORD = "Correct-Horse-Battery-42!"
PUBLIC_USER_FIELDS = {
    "uuid",
    "email",
    "phone",
    "role",
    "email_verified",
    "phone_verified",
    "created_at",
}


def assert_error(response, status_code, code=None):
    assert response.status_code == status_code
    assert set(response.json()) == {"error"}
    error = response.json()["error"]
    assert set(error) == {"code", "message", "details"}
    if code:
        assert error["code"] == code


def register(api_client, **overrides):
    payload = {
        "email": "adult@example.com",
        "phone": "+9647700000000",
        "password": PASSWORD,
    }
    payload.update(overrides)
    return api_client.post(REGISTER, payload, format="json")


def login(api_client, email="adult@example.com", password=PASSWORD):
    return api_client.post(LOGIN, {"email": email, "password": password}, format="json")


@pytest.mark.django_db
def test_successful_registration_creates_only_safe_user_account(api_client):
    response = register(api_client)

    assert response.status_code == 201
    assert set(response.json()) == {"data"}
    assert set(response.json()["data"]) == PUBLIC_USER_FIELDS
    user = get_user_model().objects.get()
    assert user.email == "adult@example.com"
    assert user.role == user.Role.PATIENT
    assert user.status == user.Status.ACTIVE
    assert user.is_active is True


@pytest.mark.django_db
def test_registration_normalizes_entire_email(api_client):
    response = register(api_client, email="  Adult.Person@EXAMPLE.COM  ")

    assert response.status_code == 201
    assert response.json()["data"]["email"] == "adult.person@example.com"
    assert get_user_model().objects.get().email == "adult.person@example.com"


@pytest.mark.django_db
def test_duplicate_email_is_rejected_case_insensitively(api_client):
    UserFactory(email="adult@example.com")

    response = register(api_client, email="ADULT@EXAMPLE.COM")

    assert_error(response, 400, "validation_error")
    assert "email" in response.json()["error"]["details"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"email": "not-an-email"}, "email"),
        ({"email": None}, "email"),
        ({"password": None}, "password"),
    ],
)
def test_registration_rejects_malformed_or_missing_fields(api_client, payload, field):
    response = register(api_client, **payload)

    assert_error(response, 400, "validation_error")
    assert field in response.json()["error"]["details"]


@pytest.mark.django_db
def test_registration_rejects_weak_password(api_client):
    response = register(api_client, password="password")

    assert_error(response, 400, "validation_error")
    assert "password" in response.json()["error"]["details"]


@pytest.mark.django_db
def test_registration_never_returns_password_and_stores_hash(api_client):
    response = register(api_client)
    user = get_user_model().objects.get()

    assert "password" not in response.json()["data"]
    assert user.password != PASSWORD
    assert user.check_password(PASSWORD) is True


@pytest.mark.django_db
@pytest.mark.parametrize(
    "privileged_fields",
    [
        {"role": "ADMIN", "status": "ACTIVE"},
        {"is_staff": True, "is_superuser": True},
        {"email_verified": True, "phone_verified": True, "is_active": False},
    ],
)
def test_registration_rejects_privilege_or_state_injection(
    api_client, privileged_fields
):
    response = register(api_client, **privileged_fields)

    assert_error(response, 400, "validation_error")
    assert set(privileged_fields) <= set(response.json()["error"]["details"])
    assert not get_user_model().objects.exists()


@pytest.mark.django_db
def test_successful_login_returns_access_and_refresh(api_client):
    UserFactory(email="adult@example.com", password=PASSWORD, status="ACTIVE")

    response = login(api_client)

    assert response.status_code == 200
    assert set(response.json()) == {"data"}
    assert set(response.json()["data"]) == {"access", "refresh"}
    assert AccessToken(response.json()["data"]["access"])["token_type"] == "access"
    assert RefreshToken(response.json()["data"]["refresh"])["token_type"] == "refresh"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("adult@example.com", "incorrect-password"),
        ("missing@example.com", PASSWORD),
    ],
)
def test_login_uses_generic_error_for_bad_password_and_unknown_email(
    api_client, email, password
):
    UserFactory(email="adult@example.com", password=PASSWORD, status="ACTIVE")

    response = login(api_client, email, password)

    assert_error(response, 401, "invalid_credentials")
    assert response.json()["error"]["message"] == "Invalid credentials."


@pytest.mark.django_db
@pytest.mark.parametrize(
    "account_fields",
    [
        {"is_active": False, "status": "ACTIVE"},
        {"status": "PENDING"},
        {"status": "SUSPENDED"},
        {"status": "DISABLED"},
    ],
)
def test_login_rejects_unavailable_account_without_disclosure(
    api_client, account_fields
):
    UserFactory(email="adult@example.com", password=PASSWORD, **account_fields)

    response = login(api_client)

    assert_error(response, 401, "invalid_credentials")
    assert response.json()["error"]["message"] == "Invalid credentials."


@pytest.mark.django_db
def test_valid_access_token_authorizes_me(api_client):
    user = UserFactory(status="ACTIVE")
    access = str(RefreshToken.for_user(user).access_token)

    response = api_client.get(ME, HTTP_AUTHORIZATION=f"Bearer {access}")

    assert response.status_code == 200
    assert set(response.json()["data"]) == PUBLIC_USER_FIELDS
    assert response.json()["data"]["uuid"] == str(user.uuid)


@pytest.mark.django_db
@pytest.mark.parametrize("authorization", [None, "Bearer malformed.jwt.token"])
def test_me_rejects_missing_or_malformed_jwt(api_client, authorization):
    kwargs = {"HTTP_AUTHORIZATION": authorization} if authorization else {}

    response = api_client.get(ME, **kwargs)

    assert_error(response, 401)


@pytest.mark.django_db
def test_expired_access_token_is_rejected(api_client):
    user = UserFactory(status="ACTIVE")
    token = AccessToken.for_user(user)
    token.set_exp(from_time=timezone.now() - timedelta(minutes=10))

    response = api_client.get(ME, HTTP_AUTHORIZATION=f"Bearer {token}")

    assert_error(response, 401, "token_not_valid")


@pytest.mark.django_db
def test_refresh_success_rotates_and_rejects_old_refresh(api_client):
    user = UserFactory(status="ACTIVE")
    original = str(RefreshToken.for_user(user))

    response = api_client.post(REFRESH, {"refresh": original}, format="json")

    assert response.status_code == 200
    assert set(response.json()["data"]) == {"access", "refresh"}
    assert response.json()["data"]["refresh"] != original
    replay = api_client.post(REFRESH, {"refresh": original}, format="json")
    assert_error(replay, 401, "token_not_valid")


@pytest.mark.django_db
@pytest.mark.parametrize("token", ["invalid-token", ""])
def test_invalid_refresh_token_is_rejected(api_client, token):
    response = api_client.post(REFRESH, {"refresh": token}, format="json")

    assert_error(response, 401 if token else 400)


@pytest.mark.django_db
def test_access_token_is_rejected_as_refresh(api_client):
    user = UserFactory(status="ACTIVE")
    access = str(AccessToken.for_user(user))

    response = api_client.post(REFRESH, {"refresh": access}, format="json")

    assert_error(response, 401, "token_not_valid")


@pytest.mark.django_db
def test_refresh_token_is_rejected_as_access(api_client):
    user = UserFactory(status="ACTIVE")
    refresh = str(RefreshToken.for_user(user))

    response = api_client.get(ME, HTTP_AUTHORIZATION=f"Bearer {refresh}")

    assert_error(response, 401, "token_not_valid")


@pytest.mark.django_db
def test_refresh_enforces_current_account_status(api_client):
    user = UserFactory(status="ACTIVE")
    refresh = str(RefreshToken.for_user(user))
    user.status = user.Status.SUSPENDED
    user.save(update_fields=["status"])

    response = api_client.post(REFRESH, {"refresh": refresh}, format="json")

    assert_error(response, 401, "account_unavailable")


@pytest.mark.django_db
def test_access_enforces_current_account_status(api_client):
    user = UserFactory(status="ACTIVE")
    access = str(RefreshToken.for_user(user).access_token)
    user.status = user.Status.SUSPENDED
    user.save(update_fields=["status"])

    response = api_client.get(ME, HTTP_AUTHORIZATION=f"Bearer {access}")

    assert_error(response, 401, "account_unavailable")


@pytest.mark.django_db
def test_access_rejects_inactive_account(api_client):
    user = UserFactory(status="ACTIVE")
    access = str(RefreshToken.for_user(user).access_token)
    user.is_active = False
    user.save(update_fields=["is_active"])

    response = api_client.get(ME, HTTP_AUTHORIZATION=f"Bearer {access}")

    assert_error(response, 401, "account_unavailable")


@pytest.mark.django_db
def test_logout_blacklists_refresh_and_prevents_reuse(api_client):
    user = UserFactory(status="ACTIVE")
    token = RefreshToken.for_user(user)
    access = str(token.access_token)
    refresh = str(token)

    response = api_client.post(
        LOGOUT,
        {"refresh": refresh},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {access}",
    )

    assert response.status_code == 200
    assert response.json() == {"data": {"message": "Logged out."}}
    replay = api_client.post(REFRESH, {"refresh": refresh}, format="json")
    assert_error(replay, 401, "token_not_valid")


@pytest.mark.django_db
def test_logout_requires_access_authentication(api_client):
    user = UserFactory(status="ACTIVE")
    refresh = str(RefreshToken.for_user(user))

    response = api_client.post(LOGOUT, {"refresh": refresh}, format="json")

    assert_error(response, 401, "not_authenticated")


@pytest.mark.django_db
def test_logout_cannot_revoke_another_accounts_refresh_token(api_client):
    attacker = UserFactory(status="ACTIVE")
    victim = UserFactory(status="ACTIVE")
    attacker_access = str(RefreshToken.for_user(attacker).access_token)
    victim_refresh = str(RefreshToken.for_user(victim))

    response = api_client.post(
        LOGOUT,
        {"refresh": victim_refresh},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {attacker_access}",
    )

    assert_error(response, 401, "token_not_valid")
    reuse = api_client.post(REFRESH, {"refresh": victim_refresh}, format="json")
    assert reuse.status_code == 200


@pytest.mark.django_db
def test_me_never_leaks_sensitive_or_internal_fields(api_client):
    user = UserFactory(status="ACTIVE", is_staff=True, email_verified=True)
    access = str(RefreshToken.for_user(user).access_token)

    response = api_client.get(ME, HTTP_AUTHORIZATION=f"Bearer {access}")

    assert set(response.json()["data"]) == PUBLIC_USER_FIELDS
    leaked = {"password", "status", "is_active", "is_staff", "is_superuser"}
    assert leaked.isdisjoint(response.json()["data"])


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("get", REGISTER),
        ("get", LOGIN),
        ("get", REFRESH),
        ("get", LOGOUT),
        ("post", ME),
    ],
)
def test_unsupported_auth_methods_return_405(api_client, method, url):
    user = UserFactory(status="ACTIVE")
    access = str(RefreshToken.for_user(user).access_token)

    response = getattr(api_client, method)(
        url, format="json", HTTP_AUTHORIZATION=f"Bearer {access}"
    )

    assert_error(response, 405, "method_not_allowed")


THROTTLE_SETTINGS = {
    "DEFAULT_THROTTLE_RATES": {"auth_register": "1/hour", "auth_login": "1/hour"},
    "EXCEPTION_HANDLER": "common.exceptions.api_exception_handler",
}


@pytest.mark.django_db
@override_settings(REST_FRAMEWORK=THROTTLE_SETTINGS)
@pytest.mark.parametrize("url", [REGISTER, LOGIN])
def test_registration_and_login_are_throttled(api_client, url):
    cache.clear()
    payload = (
        {"email": "first@example.com", "password": PASSWORD}
        if url == REGISTER
        else {"email": "missing@example.com", "password": PASSWORD}
    )
    api_client.post(url, payload, format="json", REMOTE_ADDR="198.51.100.10")

    response = api_client.post(url, payload, format="json", REMOTE_ADDR="198.51.100.10")

    assert_error(response, 429, "throttled")
