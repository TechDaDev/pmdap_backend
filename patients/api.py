from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.serializers import ErrorEnvelopeSerializer
from patients.exceptions import (
    PatientProfileExists,
    PatientProfileNotFound,
    PatientRoleRequired,
)
from patients.models import PatientProfile
from patients.serializers import (
    PatientProfileInputSerializer,
    PatientProfileSerializer,
    PatientProfileUpdateSerializer,
)
from patients.services import create_patient_profile


def profile_envelope(name):
    return inline_serializer(
        name=name,
        fields={"data": PatientProfileSerializer(read_only=True)},
    )


def require_patient(user):
    if user.role != user.Role.PATIENT:
        raise PatientRoleRequired()


def owned_profile(user):
    require_patient(user)
    try:
        return PatientProfile.objects.get(user=user)
    except PatientProfile.DoesNotExist as exc:
        raise PatientProfileNotFound() from exc


class PatientMeView(APIView):
    @extend_schema(
        responses={
            200: profile_envelope("PatientProfileSuccess"),
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
        },
        tags=["Patients"],
    )
    def get(self, request):
        profile = owned_profile(request.user)
        return Response({"data": PatientProfileSerializer(profile).data})

    @extend_schema(
        request=PatientProfileInputSerializer,
        responses={
            201: profile_envelope("PatientProfileCreated"),
            400: ErrorEnvelopeSerializer,
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            409: ErrorEnvelopeSerializer,
        },
        tags=["Patients"],
    )
    def post(self, request):
        require_patient(request.user)
        if PatientProfile.objects.filter(user=request.user).exists():
            raise PatientProfileExists()
        serializer = PatientProfileInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            profile = create_patient_profile(
                user=request.user, **serializer.validated_data
            )
        except (DjangoValidationError, IntegrityError):
            if PatientProfile.objects.filter(user=request.user).exists():
                raise PatientProfileExists() from None
            raise
        return Response(
            {"data": PatientProfileSerializer(profile).data},
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        request=PatientProfileUpdateSerializer,
        responses={
            200: profile_envelope("PatientProfileUpdated"),
            400: ErrorEnvelopeSerializer,
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
        },
        tags=["Patients"],
    )
    def patch(self, request):
        profile = owned_profile(request.user)
        serializer = PatientProfileUpdateSerializer(
            profile, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response({"data": PatientProfileSerializer(profile).data})
