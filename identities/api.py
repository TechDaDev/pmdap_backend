import logging
from django.http import FileResponse
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from accounts.serializers import ErrorEnvelopeSerializer
from identities import extraction
from identities.exceptions import (
    IdentityDocumentNotFound,
    IdentityExtractionJobNotFound,
    IdentityFileStorageFailed,
    VerificationAgentRequired,
)
from identities.extraction_store import (
    clear_extraction_result,
    read_extraction_result,
)
from identities.models import IdentityDocument, IdentityExtractionJob
from identities.storage import private_identity_storage
from identities.tasks import extract_identity_document

try:
    from botocore.exceptions import ClientError as _S3ClientError
except ImportError:  # pragma: no cover - S3 client is optional in minimal installs
    _S3ClientError = OSError

from identities.serializers import (
    EmptySerializer,
    IdentityDocumentDetailSerializer,
    IdentityDocumentInputSerializer,
    IdentityDocumentSummarySerializer,
    IdentityExtractionJobSerializer,
    IdentityExtractionRequestSerializer,
    IdentityExtractionStatusSerializer,
    RejectionSerializer,
    VerificationDetailSerializer,
    VerificationQueueFilterSerializer,
    VerificationQueueSerializer,
)
from identities.services import (
    approve_identity_document,
    finalize_identity_document,
    reject_identity_document,
    submit_identity_document,
)
from patients.api import owned_profile, require_patient

logger = logging.getLogger(__name__)

EXTRACTOR_VERSION = extraction.EXTRACTOR_VERSION


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
    parser_classes = (MultiPartParser, FormParser, JSONParser)

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
        validated_data = dict(serializer.validated_data)
        job_id = validated_data.pop("extraction_job_id", None)
        if job_id is not None:
            job = _owned_extraction_job(request.user, job_id)
            document = finalize_identity_document(
                patient=profile,
                actor=request.user,
                validated_data=validated_data,
                job=job,
            )
        else:
            document = submit_identity_document(
                patient=profile,
                actor=request.user,
                validated_data=validated_data,
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
    parser_classes = (MultiPartParser, FormParser, JSONParser)

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
        validated_data = dict(serializer.validated_data)
        job_id = validated_data.pop("extraction_job_id", None)
        if job_id is not None:
            job = _owned_extraction_job(request.user, job_id)
            document = finalize_identity_document(
                patient=source.patient,
                actor=request.user,
                validated_data=validated_data,
                job=job,
                replaces=source,
            )
        else:
            document = submit_identity_document(
                patient=source.patient,
                actor=request.user,
                validated_data=validated_data,
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


def _safe_ext(name: str) -> str:
    lower = (name or "").lower()
    if lower.endswith(".png"):
        return ".png"
    if lower.endswith(".jpeg") or lower.endswith(".jpg"):
        return ".jpg"
    if lower.endswith(".webp"):
        return ".webp"
    return ".jpg"


def _store_staging(job, filename: str, upload) -> str:
    """Write an uploaded identity image to private storage (transient staging).

    The key is job-scoped so the OCR worker can read it; the worker deletes the
    object after processing. Uploaded images are never stored in the DB.
    """
    key = f"extract_staging/{job.uuid}/{filename}"
    try:
        private_identity_storage.save(key, upload)
    except Exception as exc:
        raise IdentityFileStorageFailed() from exc
    return key


def _cleanup_staging_keys(keys):
    for key in keys:
        if not key:
            continue
        try:
            if private_identity_storage.exists(key):
                private_identity_storage.delete(key)
        except Exception:  # pragma: no cover - storage failure path
            logger.warning(
                "identity extraction staging cleanup failed for %s",
                key,
                exc_info=True,
            )


def _owned_extraction_job(user, job_uuid):
    """Fetch a job owned by [user]; 404 hides the job's existence otherwise."""
    try:
        return IdentityExtractionJob.objects.get(uuid=job_uuid, user=user)
    except IdentityExtractionJob.DoesNotExist:
        raise IdentityExtractionJobNotFound() from None


def _expire_job_for_poll(job):
    """Expire a SUCCESS job whose cached result already vanished."""
    _cleanup_staging_keys([job.front_key, job.back_key])
    clear_extraction_result(job.uuid)
    job.status = IdentityExtractionJob.Status.EXPIRED
    job.front_key = ""
    job.back_key = ""
    job.save(update_fields=["status", "front_key", "back_key", "updated_at"])
    job.delete()


class IdentityExtractionView(APIView):
    """Advisory identity extraction (async via the OCR worker queue).

    Returns a 202 with a job_id; the client polls the status endpoint. No
    IdentityDocument is created and no raw OCR text is returned or logged.
    Requires an authenticated PATIENT.
    """

    parser_classes = [MultiPartParser, FormParser]
    serializer_class = IdentityExtractionRequestSerializer

    @extend_schema(
        operation_id="identity_document_extract",
        request=IdentityExtractionRequestSerializer,
        responses={
            202: envelope("IdentityExtractionJob", IdentityExtractionJobSerializer),
            400: ErrorEnvelopeSerializer,
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            503: ErrorEnvelopeSerializer,
        },
        tags=["Identity documents"],
    )
    def post(self, request):
        require_patient(request.user)
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)

        document_type = serializer.validated_data["document_type"]
        job = IdentityExtractionJob.objects.create(
            user=request.user,
            document_type=document_type,
        )
        front = serializer.validated_data["front_image"]
        front_key = _store_staging(job, "front" + _safe_ext(front.name), front)
        back = serializer.validated_data.get("back_image")
        back_key = ""
        try:
            if back:
                back_key = _store_staging(
                    job, "back" + _safe_ext(back.name), back
                )
            job.front_key = front_key
            job.back_key = back_key
            job.save(update_fields=["front_key", "back_key", "updated_at"])
        except IdentityFileStorageFailed:
            _cleanup_staging_keys([front_key, back_key])
            job.delete()
            raise

        extract_identity_document.delay(str(job.uuid))
        return Response(
            {"data": {"job_id": str(job.uuid), "status": job.status}},
            status=status.HTTP_202_ACCEPTED,
        )


class IdentityExtractionStatusView(APIView):
    """Poll the result of an async extraction job."""

    @extend_schema(
        operation_id="identity_document_extract_status",
        responses={
            200: envelope(
                "IdentityExtractionStatus", IdentityExtractionStatusSerializer
            ),
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
        },
        tags=["Identity documents"],
    )
    def get(self, request, job_uuid):
        require_patient(request.user)
        try:
            job = IdentityExtractionJob.objects.get(
                uuid=job_uuid, user=request.user
            )
        except IdentityExtractionJob.DoesNotExist:
            raise IdentityExtractionJobNotFound() from None

        if job.status in (
            IdentityExtractionJob.Status.PENDING,
            IdentityExtractionJob.Status.PROCESSING,
        ):
            return Response(
                {"data": {"job_id": str(job.uuid), "status": job.status}}
            )

        if job.status == IdentityExtractionJob.Status.FAILED:
            data = {
                "job_id": str(job.uuid),
                "status": job.status,
                "error_code": job.error_code,
            }
            job.delete()
            return Response({"data": data})

        # SUCCESS: result lives in the cache (TTL). The job is NOT deleted and
        # the cache is NOT consumed here — the client finalizes later through
        # the document-create endpoint using extraction_job_id (single upload).
        result = read_extraction_result(job.uuid)
        if result is None:
            # Result expired while the user was reviewing: the job can no
            # longer be finalized, so expire it and force a re-extraction.
            _expire_job_for_poll(job)
            raise IdentityExtractionJobNotFound() from None
        return Response(
            {
                "data": {
                    **result,
                    "job_id": str(job.uuid),
                    "status": IdentityExtractionJob.Status.SUCCESS,
                }
            }
        )
