from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from rest_framework import serializers
from rest_framework_simplejwt.tokens import RefreshToken

from common.exceptions import InvalidCredentials
from patients.services import create_patient_profile


def normalize_email(email):
    return get_user_model().objects.normalize_email(email)


@transaction.atomic
def register_account(*, email, password, patient, phone=""):
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
    create_patient_profile(user=user, **patient)
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

    refresh = RefreshToken.for_user(user)
    return {"access": str(refresh.access_token), "refresh": str(refresh)}
