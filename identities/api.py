import logging
import os
import tempfile
from django.http import FileResponse
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from accounts.serializers import ErrorEnvelopeSerializer
from identities import extraction
from identities.exceptions import (
    IdentityDocumentNotFound,
    IdentityFileStorageFailed,
    VerificationAgentRequired,
)
from identities.models import IdentityDocument

try:
    from botocore.exceptions import ClientError as _S3ClientError
except ImportError:  # pragma: no cover - S3 client is optional in minimal installs
    _S3ClientError = OSError

from identities.serializers import (
    EmptySerializer,
    IdentityDocumentDetailSerializer,
    IdentityDocumentInputSerializer,
    IdentityDocumentSummarySerializer,
    IdentityExtractionRequestSerializer,
    IdentityExtractionResponseSerializer,
    RejectionSerializer,
    VerificationDetailSerializer,
    VerificationQueueFilterSerializer,
    VerificationQueueSerializer,
)
from identities.services import (
    approve_identity_document,
    reject_identity_document,
    submit_identity_document,
)
from patients.api import owned_profile, require_patient

logger = logging.getLogger(__name__)

EXTRACTOR_VERSION = "identity-v1"


def envelope(name, child):
    return inline_serializer(name=name, fields={"data": child})


def paginated_envelope(name, child):
    page = inline_serializer(
        name=f"{name}Page",
        fields={
            "count": serializers.IntegerField(),
            "next": serializers.URLField(allow_null=True),
            "previous": serializers.URLField(allow_null=True),
            "results": child,
        },
    )
    return envelope(name, page)


def require_verification_agent(user):
    if user.role != User.Role.IDENTITY_VERIFICATION_AGENT:
        raise VerificationAgentRequired()


def patient_document(user, document_uuid):
    from guardians.services import guardian_can_access_minor
    from patients.api import require_patient
    from patients.models import PatientProfile

    require_patient(user)
    try:
        document = IdentityDocument.objects.select_related(
            "front_image", "back_image", "replaces"
        ).get(uuid=document_uuid)
    except (IdentityDocument.DoesNotExist, ValueError) as exc:
        raise IdentityDocumentNotFound() from exc
    owns_document = PatientProfile.objects.filter(
        user=user, pk=document.patient_id
    ).exists()
    if owns_document or guardian_can_access_minor(user, document.patient):
        return document
    raise IdentityDocumentNotFound()


def verification_document(user, document_uuid):
    require_verification_agent(user)
    try:
        return IdentityDocument.objects.select_related(
            "patient", "front_image", "back_image", "replaces"
        ).get(uuid=document_uuid)
    except (IdentityDocument.DoesNotExist, ValueError) as exc:
        raise IdentityDocumentNotFound() from exc


def page_response(request, queryset, serializer_class):
    paginator = PageNumberPagination()
    paginator.page_size = 20
    page = paginator.paginate_queryset(queryset, request)
    return Response(
        {
            "data": {
                "count": paginator.page.paginator.count,
                "next": paginator.get_next_link(),
                "previous": paginator.get_previous_link(),
                "results": serializer_class(page, many=True).data,
            }
        }
    )


class IdentityDocumentCollectionView(APIView):
    parser_classes = (MultiPartParser, FormParser)

    @extend_schema(
        operation_id="identity_document_list",
        responses={
            200: paginated_envelope(
                "IdentityDocumentListSuccess",
                IdentityDocumentSummarySerializer(many=True),
            ),
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
        },
        tags=["Identity documents"],
    )
    def get(self, request):
        profile = owned_profile(request.user)
        queryset = IdentityDocument.objects.filter(patient=profile)
        return page_response(request, queryset, IdentityDocumentSummarySerializer)

    @extend_schema(
        operation_id="identity_document_create",
        request=IdentityDocumentInputSerializer,
        responses={
            201: envelope(
                "IdentityDocumentCreated",
                IdentityDocumentDetailSerializer(read_only=True),
            ),
            400: ErrorEnvelopeSerializer,
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
            409: ErrorEnvelopeSerializer,
        },
        tags=["Identity documents"],
    )
    def post(self, request):
        profile = owned_profile(request.user)
        serializer = IdentityDocumentInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        document = submit_identity_document(
            patient=profile,
            actor=request.user,
            validated_data=dict(serializer.validated_data),
        )
        return Response(
            {"data": IdentityDocumentDetailSerializer(document).data},
            status=status.HTTP_201_CREATED,
        )


class IdentityDocumentDetailView(APIView):
    @extend_schema(
        operation_id="identity_document_retrieve",
        responses={
            200: envelope(
                "IdentityDocumentDetailSuccess",
                IdentityDocumentDetailSerializer(read_only=True),
            ),
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
        },
        tags=["Identity documents"],
    )
    def get(self, request, document_uuid):
        document = patient_document(request.user, document_uuid)
        return Response({"data": IdentityDocumentDetailSerializer(document).data})


class IdentityDocumentReplaceView(APIView):
    parser_classes = (MultiPartParser, FormParser)

    @extend_schema(
        operation_id="identity_document_replace",
        request=IdentityDocumentInputSerializer,
        responses={
            201: envelope(
                "IdentityDocumentReplacementCreated",
                IdentityDocumentDetailSerializer(read_only=True),
            ),
            400: ErrorEnvelopeSerializer,
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
            409: ErrorEnvelopeSerializer,
        },
        tags=["Identity documents"],
    )
    def post(self, request, document_uuid):
        source = patient_document(request.user, document_uuid)
        serializer = IdentityDocumentInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        document = submit_identity_document(
            patient=source.patient,
            actor=request.user,
            validated_data=dict(serializer.validated_data),
            replaces=source,
        )
        return Response(
            {"data": IdentityDocumentDetailSerializer(document).data},
            status=status.HTTP_201_CREATED,
        )


class IdentityDocumentImageView(APIView):
    @extend_schema(
        operation_id="identity_document_image_retrieve",
        parameters=[
            OpenApiParameter(
                name="side",
                type=str,
                location=OpenApiParameter.PATH,
                required=True,
                enum=["front", "back"],
                description="Which identity image to stream: front or back.",
            )
        ],
        responses={
            (200, "image/jpeg"): bytes,
            (200, "image/png"): bytes,
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
        },
        tags=["Identity documents"],
    )
    def get(self, request, document_uuid, side):
        if request.user.role == User.Role.IDENTITY_VERIFICATION_AGENT:
            document = verification_document(request.user, document_uuid)
        else:
            document = patient_document(request.user, document_uuid)
        identity_file = document.front_image if side == "front" else document.back_image
        if side not in {"front", "back"} or identity_file is None:
            raise IdentityDocumentNotFound()
        try:
            handle = identity_file.file.open("rb")
        except (OSError, _S3ClientError) as exc:
            raise IdentityFileStorageFailed() from exc
        extension = ".png" if identity_file.media_type == "image/png" else ".jpg"
        return FileResponse(
            handle,
            content_type=identity_file.media_type,
            as_attachment=True,
            filename=f"identity-{side}{extension}",
        )


class VerificationCollectionView(APIView):
    @extend_schema(
        operation_id="identity_verification_queue_list",
        parameters=[
            OpenApiParameter(
                name="status",
                type=str,
                enum=list(IdentityDocument.VerificationStatus.values),
                required=False,
            )
        ],
        responses={
            200: paginated_envelope(
                "VerificationQueueSuccess", VerificationQueueSerializer(many=True)
            ),
            400: ErrorEnvelopeSerializer,
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
        },
        tags=["Identity verification"],
    )
    def get(self, request):
        require_verification_agent(request.user)
        filters = VerificationQueueFilterSerializer(data=request.query_params)
        filters.is_valid(raise_exception=True)
        queryset = IdentityDocument.objects.select_related("patient")
        if filter_status := filters.validated_data.get("status"):
            queryset = queryset.filter(verification_status=filter_status)
        return page_response(request, queryset, VerificationQueueSerializer)


class VerificationDetailView(APIView):
    @extend_schema(
        operation_id="identity_verification_document_retrieve",
        responses={
            200: envelope(
                "VerificationDetailSuccess",
                VerificationDetailSerializer(read_only=True),
            ),
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
        },
        tags=["Identity verification"],
    )
    def get(self, request, document_uuid):
        document = verification_document(request.user, document_uuid)
        return Response({"data": VerificationDetailSerializer(document).data})


class VerificationApproveView(APIView):
    @extend_schema(
        operation_id="identity_verification_document_approve",
        request=EmptySerializer,
        responses={
            200: envelope(
                "VerificationApproved",
                VerificationDetailSerializer(read_only=True),
            ),
            400: ErrorEnvelopeSerializer,
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
            409: ErrorEnvelopeSerializer,
        },
        tags=["Identity verification"],
    )
    def post(self, request, document_uuid):
        document = verification_document(request.user, document_uuid)
        serializer = EmptySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        document = approve_identity_document(document=document, agent=request.user)
        return Response({"data": VerificationDetailSerializer(document).data})


class VerificationRejectView(APIView):
    @extend_schema(
        operation_id="identity_verification_document_reject",
        request=RejectionSerializer,
        responses={
            200: envelope(
                "VerificationRejected",
                VerificationDetailSerializer(read_only=True),
            ),
            400: ErrorEnvelopeSerializer,
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
            409: ErrorEnvelopeSerializer,
        },
        tags=["Identity verification"],
    )
    def post(self, request, document_uuid):
        document = verification_document(request.user, document_uuid)
        serializer = RejectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        document = reject_identity_document(
            document=document,
            agent=request.user,
            reason=serializer.validated_data["rejection_reason"],
        )
        return Response({"data": VerificationDetailSerializer(document).data})


def _write_upload(upload, path: str) -> None:
    with open(path, "wb") as out:
        for chunk in upload.chunks():
            out.write(chunk)


def _safe_ext(name: str) -> str:
    lower = (name or "").lower()
    if lower.endswith(".png"):
        return ".png"
    if lower.endswith(".jpeg") or lower.endswith(".jpg"):
        return ".jpg"
    if lower.endswith(".webp"):
        return ".webp"
    return ".jpg"


def _confidence_bucket(conf: float) -> str:
    if conf >= 0.90:
        return "high"
    if conf >= 0.70:
        return "medium"
    return "low"


class IdentityExtractionView(APIView):
    """Advisory identity extraction (no IdentityDocument is created, no raw OCR
    text is returned or logged). Requires an authenticated PATIENT."""

    parser_classes = [MultiPartParser, FormParser]
    serializer_class = IdentityExtractionRequestSerializer

    @extend_schema(
        operation_id="identity_document_extract",
        request=IdentityExtractionRequestSerializer,
        responses={
            200: envelope("IdentityExtraction", IdentityExtractionResponseSerializer),
            400: ErrorEnvelopeSerializer,
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
        },
        tags=["Identity documents"],
    )
    def post(self, request):
        require_patient(request.user)
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)

        document_type = serializer.validated_data["document_type"]
        with tempfile.TemporaryDirectory(prefix="pmdap_extract_") as tmp:
            front = serializer.validated_data["front_image"]
            front_path = os.path.join(tmp, "front" + _safe_ext(front.name))
            _write_upload(front, front_path)
            back = serializer.validated_data.get("back_image")
            back_path = None
            if back:
                back_path = os.path.join(tmp, "back" + _safe_ext(back.name))
                _write_upload(back, back_path)

            lines = extraction.ocr_text(front_path)
            if back_path:
                lines.extend(extraction.ocr_text(back_path))
            fields, warnings, mrz_summary = extraction.extract_identity(
                document_type, lines
            )

        # Safe log: endpoint, type, status, field names + confidence buckets.
        # NEVER log values / raw OCR / image bytes.
        bucket_summary = {name: _confidence_bucket(f["confidence"]) for name, f in fields.items()}
        logger.info(
            "POST /identity-documents/extract/ type=%s ok fields=%s mrz=%s",
            document_type,
            bucket_summary,
            mrz_summary.get("detected"),
        )
        return Response(
            {
                "data": {
                    "document_type": document_type,
                    "extractor_version": EXTRACTOR_VERSION,
                    "fields": fields,
                    "warnings": warnings,
                    "mrz": mrz_summary,
                }
            }
        )
