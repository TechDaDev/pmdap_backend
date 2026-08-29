from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from rest_framework import serializers
from rest_framework_simplejwt.tokens import RefreshToken

from audit.models import AuditLog
from audit.services import record_audit
from common.exceptions import InvalidCredentials
from patients.services import create_patient_profile

SESSION_VERSION_CLAIM = "session_version"


def normalize_email(email):
    return get_user_model().objects.normalize_email(email)


@transaction.atomic
def register_account(
    *,
    email,
    password,
    patient=None,
    phone="",
    governorate="",
    registration_identity=None,
    registration_session=None,
):
    if registration_identity is not None:
        from registration.services import finalize_scan_first_registration

        return finalize_scan_first_registration(
            email=email,
            password=password,
            phone=phone,
            governorate=governorate,
            registration_identity=registration_identity,
            registration_session=registration_session,
        )
    if patient is None:
        raise serializers.ValidationError(
            {"patient": ["Patient demographics are required for manual registration."]}
        )

    user_model = get_user_model()
    try:
        user = user_model.objects.create_user(
            email=email,
            password=password,
            phone=phone,
            role=user_model.Role.PATIENT,
            status=user_model.Status.ACTIVE,
            is_active=True,
            is_staff=False,
            is_superuser=False,
            email_verified=False,
            phone_verified=False,
        )
    except IntegrityError as exc:
        raise serializers.ValidationError(
            {"email": ["An account with this email already exists."]}
        ) from exc
    profile = create_patient_profile(user=user, **patient)
    record_audit(
        action=AuditLog.Action.ACCOUNT_CREATED,
        actor=user,
        patient=profile,
        resource_type="USER",
        resource_uuid=user.uuid,
        new_values={"role": user.role, "status": user.status},
    )
    return user


def issue_tokens(*, email, password):
    user_model = get_user_model()
    normalized_email = normalize_email(email)
    try:
        user = user_model.objects.get(email__iexact=normalized_email)
    except user_model.DoesNotExist:
        user_model().set_password(password)
        raise InvalidCredentials() from None

    if (
        not user.check_password(password)
        or not user.is_active
        or user.status != user.Status.ACTIVE
    ):
        raise InvalidCredentials()

    return issue_tokens_for_user(user)


def issue_tokens_for_user(user):
    """Issue a fresh token pair carrying the user's current session version.

    Used by login and by flows that rotate sessions (e.g. password change)
    without requiring the password again.
    """
    refresh = RefreshToken.for_user(user)
    refresh[SESSION_VERSION_CLAIM] = user.auth_session_version
    return {"access": str(refresh.access_token), "refresh": str(refresh)}


def token_matches_current_session(token, user):
    try:
        token_version = int(token.get(SESSION_VERSION_CLAIM, 0))
    except (TypeError, ValueError):
        return False
    return token_version == user.auth_session_version
