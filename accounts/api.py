from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.authentication import ActiveAccountJWTAuthentication
from identities.models import IdentityDocument
from patients.models import PatientProfile
from accounts.serializers import (
    ErrorEnvelopeSerializer,
    LoginSerializer,
    LogoutSerializer,
    MessageSerializer,
    PublicUserSerializer,
    RefreshSerializer,
    RegisterSerializer,
    TokenPairSerializer,
)
from accounts.throttles import LoginRateThrottle, RegisterRateThrottle


def envelope(name, child):
    return inline_serializer(name=name, fields={"data": child})


ERRORS = {400: ErrorEnvelopeSerializer, 401: ErrorEnvelopeSerializer}


class RegisterView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [RegisterRateThrottle]
    throttle_scope = "auth_register"

    @extend_schema(
        request=RegisterSerializer,
        responses={
            201: envelope("RegisterSuccess", PublicUserSerializer(read_only=True)),
            400: ErrorEnvelopeSerializer,
            429: ErrorEnvelopeSerializer,
        },
        tags=["Authentication"],
    )
    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        data = PublicUserSerializer(user).data
        # Scan-first registration: include a least-disclosure patient +
        # identity summary (never the card/family/body identifiers). Re-query
        # fresh — the user instance's cached reverse relation predates the
        # PENDING_VERIFICATION status sync.
        if request.data.get("registration_identity") is not None:
            profile = (
                PatientProfile.objects.filter(user=user).select_related().first()
            )
            if profile is not None:
                data["patient"] = {
                    "uuid": str(profile.uuid),
                    "digital_id": profile.digital_id,
                    "identity_status": profile.identity_status,
                }
                doc = (
                    IdentityDocument.objects.filter(patient=profile)
                    .order_by("-created_at")
                    .first()
                )
                if doc is not None:
                    data["identity_document"] = {
                        "uuid": str(doc.uuid),
                        "status": doc.status,
                        "verification_status": doc.verification_status,
                    }
        return Response(
            {"data": data}, status=status.HTTP_201_CREATED
        )


class LoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [LoginRateThrottle]
    throttle_scope = "auth_login"

    @extend_schema(
        request=LoginSerializer,
        responses={
            200: envelope("LoginSuccess", TokenPairSerializer(read_only=True)),
            **ERRORS,
            429: ErrorEnvelopeSerializer,
        },
        tags=["Authentication"],
    )
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response({"data": serializer.validated_data})


class RefreshView(APIView):
    authentication_classes = [ActiveAccountJWTAuthentication]
    permission_classes = [AllowAny]

    @extend_schema(
        request=RefreshSerializer,
        responses={
            200: envelope("RefreshSuccess", TokenPairSerializer(read_only=True)),
            **ERRORS,
        },
        tags=["Authentication"],
    )
    def post(self, request):
        serializer = RefreshSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response({"data": serializer.validated_data})


class LogoutView(APIView):
    @extend_schema(
        request=LogoutSerializer,
        responses={
            200: envelope("LogoutSuccess", MessageSerializer(read_only=True)),
            **ERRORS,
        },
        tags=["Authentication"],
    )
    def post(self, request):
        serializer = LogoutSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        return Response({"data": {"message": "Logged out."}})


class MeView(APIView):
    @extend_schema(
        responses={
            200: envelope("MeSuccess", PublicUserSerializer(read_only=True)),
            401: ErrorEnvelopeSerializer,
        },
        tags=["Authentication"],
    )
    def get(self, request):
        return Response({"data": PublicUserSerializer(request.user).data})
