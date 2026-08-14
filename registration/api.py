"""Public scan-first registration identity endpoints (anonymous, throttled).

Extraction values are ADVISORY suggestions only; the review/correction happens
in the Flutter client and the final register carries the human-confirmed
values. Job ownership is a capability token (job_id + job_token); possession of
job_id alone is never sufficient.
"""
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.serializers import ErrorEnvelopeSerializer
from registration.exceptions import RegistrationIdentityJobExpired
from registration.models import RegistrationIdentityExtractionJob
from registration.serializers import (
    RegistrationIdentityExtractRequestSerializer,
    RegistrationIdentityStatusSerializer,
)
from registration.services import (
    _expire_job,
    get_job_for_poll,
    issue_registration_job,
    read_registration_result,
)
from registration.tasks import process_registration_identity_extraction
from registration.throttles import (
    RegistrationIdentityExtractRateThrottle,
    RegistrationIdentityPollRateThrottle,
)


def envelope(name, child):
    return inline_serializer(name=name, fields={"data": child})


class RegistrationIdentityExtractView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    parser_classes = (MultiPartParser, FormParser)
    throttle_classes = [RegistrationIdentityExtractRateThrottle]
    throttle_scope = "registration_identity_extract"

    @extend_schema(
        operation_id="registration_identity_extract",
        request=RegistrationIdentityExtractRequestSerializer,
        responses={
            202: envelope(
                "RegistrationIdentityExtractCreated",
                inline_serializer(
                    name="RegistrationIdentityExtractCreatedData",
                    fields={
                        "job_id": serializers.UUIDField(),
                        "job_token": serializers.CharField(),
                        "status": serializers.CharField(),
                    },
                ),
            ),
            400: ErrorEnvelopeSerializer,
            429: ErrorEnvelopeSerializer,
            503: ErrorEnvelopeSerializer,
        },
        tags=["Registration"],
        description=(
            "Public scan-first extraction. Upload the Unified National Card "
            "front/back ONCE. Returns a capability (job_id + job_token) for "
            "polling and final registration. Extracted values are advisory "
            "only and must be human-reviewed."
        ),
    )
    def post(self, request):
        serializer = RegistrationIdentityExtractRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        job, token = issue_registration_job(
            document_type=serializer.validated_data["document_type"],
            front_upload=serializer.validated_data["front_image"],
            back_upload=serializer.validated_data["back_image"],
        )
        process_registration_identity_extraction.delay(str(job.uuid))
        return Response(
            {
                "data": {
                    "job_id": str(job.uuid),
                    "job_token": token,
                    "status": job.status,
                }
            },
            status=status.HTTP_202_ACCEPTED,
        )


class RegistrationIdentityStatusView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [RegistrationIdentityPollRateThrottle]
    throttle_scope = "registration_identity_poll"

    @extend_schema(
        operation_id="registration_identity_extract_status",
        responses={
            200: envelope(
                "RegistrationIdentityStatus",
                RegistrationIdentityStatusSerializer,
            ),
            400: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
            409: ErrorEnvelopeSerializer,
            410: ErrorEnvelopeSerializer,
            429: ErrorEnvelopeSerializer,
        },
        tags=["Registration"],
        description=(
            "Poll a scan-first extraction. The capability token is sent in the "
            "X-Registration-Job-Token header (never in the URL). A wrong or "
            "missing token returns 404 without revealing whether the job "
            "exists. On SUCCESS returns the advisory fields/warnings/MRZ."
        ),
    )
    def get(self, request, job_id):
        token = request.headers.get("X-Registration-Job-Token") or ""
        job = get_job_for_poll(job_id=job_id, token=token)

        if job.status in (
            RegistrationIdentityExtractionJob.Status.PENDING,
            RegistrationIdentityExtractionJob.Status.PROCESSING,
        ):
            return Response(
                {"data": {"job_id": str(job.uuid), "status": job.status}}
            )

        if job.status == RegistrationIdentityExtractionJob.Status.FAILED:
            data = {
                "job_id": str(job.uuid),
                "status": job.status,
                "error_code": job.error_code,
            }
            job.delete()
            return Response({"data": data})

        # SUCCESS: result lives in the cache (TTL), staging retained for the
        # single-upload final register.
        result = read_registration_result(job.uuid)
        if result is None:
            _expire_job(job)
            raise RegistrationIdentityJobExpired()
        return Response(
            {
                "data": {
                    **result,
                    "job_id": str(job.uuid),
                    "status": job.status,
                }
            }
        )
