from django.http import FileResponse
from drf_spectacular.utils import extend_schema, extend_schema_view, inline_serializer
from rest_framework import serializers, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.serializers import ErrorEnvelopeSerializer
from documents.date_services import confirm_document_date, pending_confirmation_queryset
from documents.exceptions import (
    MedicalDocumentNotFound,
    MedicalFileStorageFailed,
    MedicalFileUnavailable,
)
from documents.models import MedicalDocument, StoredFile

try:
    from botocore.exceptions import ClientError as _S3ClientError
except ImportError:  # pragma: no cover - S3 client is optional in minimal installs
    _S3ClientError = OSError

from documents.serializers import (
    DateCandidateSerializer,
    DocumentDateConfirmationResponseSerializer,
    DocumentDateConfirmationSerializer,
    ExtractedContentResponseSerializer,
    ExtractedContentSectionSerializer,
    PendingDateConfirmationSerializer,
    MedicalDocumentDetailSerializer,
    MedicalDocumentMetadataSerializer,
    MedicalDocumentSerializer,
    MedicalDocumentUploadSerializer,
)
from documents.narrative import extract_narrative
from documents.services import (
    create_medical_document,
    soft_delete_medical_document,
    update_medical_document,
)
from documents.throttling import MedicalDocumentUploadThrottle
from labs.models import LabReportExtraction
from patients.api import owned_profile


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


def active_document(patient, document_uuid):
    try:
        return MedicalDocument.objects.select_related(
            "stored_file",
            "document_text",
            "healthcare_facility__country",
            "healthcare_facility__region",
            "healthcare_facility__city",
        ).get(
            uuid=document_uuid,
            patient=patient,
            archive_status=MedicalDocument.ArchiveStatus.ACTIVE,
        )
    except (MedicalDocument.DoesNotExist, ValueError) as exc:
        raise MedicalDocumentNotFound() from exc


def page_response(request, queryset, serializer_class=MedicalDocumentSerializer):
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


def stream_document(document):
    if document.stored_file.integrity_status in {
        StoredFile.IntegrityStatus.CORRUPTED,
        StoredFile.IntegrityStatus.QUARANTINED,
        StoredFile.IntegrityStatus.MISSING,
    }:
        raise MedicalFileUnavailable()
    try:
        original = document.stored_file.file.open("rb")
    except (OSError, _S3ClientError) as exc:
        raise MedicalFileStorageFailed() from exc
    response = FileResponse(
        original,
        as_attachment=True,
        filename=document.stored_file.original_filename,
        content_type=document.stored_file.mime_type,
    )
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


class MedicalDocumentCollectionView(APIView):
    parser_classes = (MultiPartParser, FormParser)

    def get_throttles(self):
        if self.request.method == "POST":
            return [MedicalDocumentUploadThrottle()]
        return []

    def get_patient(self, request, **kwargs):
        del kwargs
        return owned_profile(request.user)

    @extend_schema(
        operation_id="medical_document_list",
        responses={
            200: paginated_envelope(
                "MedicalDocumentListSuccess",
                MedicalDocumentSerializer(many=True),
            ),
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
        },
        tags=["Medical documents"],
    )
    def get(self, request, **kwargs):
        patient = self.get_patient(request, **kwargs)
        queryset = (
            MedicalDocument.objects.select_related(
                "stored_file",
                "healthcare_facility__country",
                "healthcare_facility__region",
                "healthcare_facility__city",
            )
            .prefetch_related("healthcare_facility__aliases")
            .filter(
                patient=patient,
                archive_status=MedicalDocument.ArchiveStatus.ACTIVE,
            )
        )
        return page_response(request, queryset)

    @extend_schema(
        operation_id="medical_document_create",
        request=MedicalDocumentUploadSerializer,
        responses={
            201: envelope(
                "MedicalDocumentCreated",
                MedicalDocumentSerializer(read_only=True),
            ),
            400: ErrorEnvelopeSerializer,
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
            409: ErrorEnvelopeSerializer,
            429: ErrorEnvelopeSerializer,
            503: ErrorEnvelopeSerializer,
        },
        tags=["Medical documents"],
    )
    def post(self, request, **kwargs):
        patient = self.get_patient(request, **kwargs)
        serializer = MedicalDocumentUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        upload = data.pop("file")
        document = create_medical_document(
            patient=patient,
            actor=request.user,
            upload=upload,
            metadata=data,
        )
        return Response(
            {"data": MedicalDocumentSerializer(document).data},
            status=status.HTTP_201_CREATED,
        )


class MedicalDocumentDetailView(APIView):
    parser_classes = (JSONParser,)

    def get_patient(self, request, **kwargs):
        del kwargs
        return owned_profile(request.user)

    def get_document(self, request, document_uuid, **kwargs):
        return active_document(self.get_patient(request, **kwargs), document_uuid)

    @extend_schema(
        operation_id="medical_document_retrieve",
        responses={
            200: envelope(
                "MedicalDocumentDetailSuccess",
                MedicalDocumentDetailSerializer(read_only=True),
            ),
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
        },
        tags=["Medical documents"],
    )
    def get(self, request, document_uuid, **kwargs):
        return Response(
            {
                "data": MedicalDocumentDetailSerializer(
                    self.get_document(request, document_uuid, **kwargs)
                ).data
            }
        )

    @extend_schema(
        operation_id="medical_document_update",
        request=MedicalDocumentMetadataSerializer,
        responses={
            200: envelope(
                "MedicalDocumentUpdated",
                MedicalDocumentSerializer(read_only=True),
            ),
            400: ErrorEnvelopeSerializer,
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
            409: ErrorEnvelopeSerializer,
        },
        tags=["Medical documents"],
    )
    def patch(self, request, document_uuid, **kwargs):
        document = self.get_document(request, document_uuid, **kwargs)
        serializer = MedicalDocumentMetadataSerializer(
            data=request.data,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)
        document = update_medical_document(
            document=document,
            actor=request.user,
            metadata=dict(serializer.validated_data),
        )
        return Response({"data": MedicalDocumentSerializer(document).data})

    @extend_schema(
        operation_id="medical_document_delete",
        responses={
            204: None,
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
        },
        tags=["Medical documents"],
    )
    def delete(self, request, document_uuid, **kwargs):
        soft_delete_medical_document(
            document=self.get_document(request, document_uuid, **kwargs),
            actor=request.user,
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class MedicalDocumentFileView(APIView):
    def get_patient(self, request, **kwargs):
        del kwargs
        return owned_profile(request.user)

    @extend_schema(
        operation_id="medical_document_file_retrieve",
        responses={
            (200, "application/octet-stream"): bytes,
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
            409: ErrorEnvelopeSerializer,
            503: ErrorEnvelopeSerializer,
        },
        tags=["Medical documents"],
    )
    def get(self, request, document_uuid, **kwargs):
        document = active_document(
            self.get_patient(request, **kwargs),
            document_uuid,
        )
        return stream_document(document)


class MedicalDocumentDateCandidateView(APIView):
    def get_patient(self, request, **kwargs):
        del kwargs
        return owned_profile(request.user)

    @extend_schema(
        operation_id="medical_document_date_candidate_list",
        responses={
            200: paginated_envelope(
                "MedicalDocumentDateCandidateListSuccess",
                DateCandidateSerializer(many=True),
            ),
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
        },
        tags=["Medical documents"],
    )
    def get(self, request, document_uuid, **kwargs):
        document = active_document(
            self.get_patient(request, **kwargs),
            document_uuid,
        )
        return page_response(
            request,
            document.date_candidates.filter(is_current=True),
            DateCandidateSerializer,
        )


class MedicalDocumentDateConfirmationView(APIView):
    parser_classes = (JSONParser,)

    def get_patient(self, request, **kwargs):
        del kwargs
        return owned_profile(request.user)

    @extend_schema(
        operation_id="medical_document_date_confirm",
        request=DocumentDateConfirmationSerializer,
        responses={
            200: envelope(
                "MedicalDocumentDateConfirmed",
                DocumentDateConfirmationResponseSerializer(read_only=True),
            ),
            400: ErrorEnvelopeSerializer,
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
            409: ErrorEnvelopeSerializer,
        },
        tags=["Medical documents"],
    )
    def post(self, request, document_uuid, **kwargs):
        document = active_document(
            self.get_patient(request, **kwargs),
            document_uuid,
        )
        serializer = DocumentDateConfirmationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        document = confirm_document_date(
            document=document,
            actor=request.user,
            candidate_id=serializer.validated_data.get("candidate_id"),
            manual_date=serializer.validated_data.get("date"),
        )
        return Response(
            {"data": DocumentDateConfirmationResponseSerializer(document).data}
        )


class MedicalDocumentPendingConfirmationView(APIView):
    """Document-centric date-confirmation queue.

    Each active AWAITING_CONFIRMATION document is returned even when OCR found
    no date (empty `detected_candidates` + `requires_manual_date` true). The
    queue and its count derive from the SAME domain rule so Home badge and the
    queue page can never drift.
    """

    def get_patient(self, request, **kwargs):
        del kwargs
        return owned_profile(request.user)

    @extend_schema(
        operation_id="medical_document_date_confirmations_pending",
        responses={
            200: envelope(
                "MedicalDocumentPendingConfirmations",
                inline_serializer(
                    name="MedicalDocumentPendingConfirmationsData",
                    fields={
                        "count": serializers.IntegerField(),
                        "results": PendingDateConfirmationSerializer(many=True),
                    },
                ),
            ),
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
        },
        tags=["Medical documents"],
    )
    def get(self, request, **kwargs):
        patient = self.get_patient(request, **kwargs)
        queryset = pending_confirmation_queryset(patient).order_by(
            "-created_at", "-uuid"
        )
        results = []
        for document in queryset:
            rows = document.date_candidates.filter(is_current=True).order_by(
                "-score", "uuid"
            )
            # Canonical candidate shape (serializer field names) — never raw
            # OCR context. Empty list for a zero-candidate document, which is
            # still returned (manual date fallback).
            candidates = [
                {
                    "uuid": row.uuid,
                    "date": row.detected_date,
                    "confidence": row.score,
                    "type": row.candidate_type,
                    "ambiguous": row.ambiguous,
                    "is_suggested": row.is_suggested,
                }
                for row in rows
            ]
            results.append(
                {
                    "document_uuid": document.uuid,
                    "document_type": document.document_type,
                    "processing_status": document.processing_status,
                    "created_at": document.created_at,
                    "detected_candidates": candidates,
                    "requires_manual_date": not candidates,
                }
            )
        return Response({"data": {"count": len(results), "results": results}})


def authorized_minor_patient(request, minor_uuid):
    from guardians.api import authorized_minor_relationship
    from guardians.exceptions import GuardianNotVerified, GuardianRelationshipNotFound

    try:
        return authorized_minor_relationship(request.user, minor_uuid).minor_patient
    except GuardianNotVerified as exc:
        raise GuardianRelationshipNotFound() from exc


@extend_schema_view(
    get=extend_schema(operation_id="minor_medical_document_list"),
    post=extend_schema(operation_id="minor_medical_document_create"),
)
class MinorMedicalDocumentCollectionView(MedicalDocumentCollectionView):
    def get_patient(self, request, **kwargs):
        return authorized_minor_patient(request, kwargs["minor_uuid"])


@extend_schema_view(
    get=extend_schema(operation_id="minor_medical_document_retrieve"),
    patch=extend_schema(operation_id="minor_medical_document_update"),
    delete=extend_schema(operation_id="minor_medical_document_delete"),
)
class MinorMedicalDocumentDetailView(MedicalDocumentDetailView):
    def get_patient(self, request, **kwargs):
        return authorized_minor_patient(request, kwargs["minor_uuid"])


@extend_schema_view(
    get=extend_schema(operation_id="minor_medical_document_file_retrieve"),
)
class MinorMedicalDocumentFileView(MedicalDocumentFileView):
    def get_patient(self, request, **kwargs):
        return authorized_minor_patient(request, kwargs["minor_uuid"])


@extend_schema_view(
    get=extend_schema(operation_id="minor_medical_document_date_candidate_list"),
)
class MinorMedicalDocumentDateCandidateView(MedicalDocumentDateCandidateView):
    def get_patient(self, request, **kwargs):
        return authorized_minor_patient(request, kwargs["minor_uuid"])


@extend_schema_view(
    post=extend_schema(operation_id="minor_medical_document_date_confirm"),
)
class MinorMedicalDocumentDateConfirmationView(MedicalDocumentDateConfirmationView):
    def get_patient(self, request, **kwargs):
        return authorized_minor_patient(request, kwargs["minor_uuid"])


@extend_schema_view(
    get=extend_schema(
        operation_id="minor_medical_document_date_confirmations_pending"
    ),
)
class MinorMedicalDocumentPendingConfirmationView(
    MedicalDocumentPendingConfirmationView
):
    def get_patient(self, request, **kwargs):
        return authorized_minor_patient(request, kwargs["minor_uuid"])


class ExtractedContentView(APIView):
    """Read-only extracted content for one owned document.

    Narrative reports (radiology, imaging, letters) return sectioned body
    text rebuilt from persisted OCR spans. Structured lab documents return
    ``content_kind=LAB`` (the client reads the dedicated lab-results endpoint).
    Owner-only; verification agents (no patient profile) are rejected with 403
    and other patients' documents stay opaque (404). No raw geometry, no
    storage keys, no OCR confidence is ever returned.
    """

    def get_patient(self, request, **kwargs):
        del kwargs
        return owned_profile(request.user)

    def get_document(self, request, document_uuid, **kwargs):
        return active_document(self.get_patient(request, **kwargs), document_uuid)

    @extend_schema(
        operation_id="medical_document_extracted_content",
        summary="Extracted content (narrative) for one document",
        description=(
            "Read-only, owner-only. Returns narrative report sections rebuilt "
            "from the patient's own uploaded report. Never returns raw OCR "
            "geometry. Synthetic example only."
        ),
        responses={
            200: envelope(
                "ExtractedContentSuccess",
                ExtractedContentResponseSerializer(read_only=True),
            ),
            401: inline_serializer(
                "Unauthorized",
                fields={"detail": "string"},
            ),
            403: inline_serializer(
                "Forbidden",
                fields={"detail": "string"},
            ),
            404: inline_serializer(
                "NotFound",
                fields={"detail": "string"},
            ),
        },
        tags=["Medical documents"],
    )
    def get(self, request, document_uuid, **kwargs):
        document = self.get_document(request, document_uuid, **kwargs)
        return Response({"data": self._payload(document)})

    @staticmethod
    def _payload(document):
        if document.document_type == MedicalDocument.DocumentType.LABORATORY:
            extraction = (
                LabReportExtraction.objects.filter(document=document)
                .order_by("-created_at")
                .first()
            )
            if extraction is not None:
                status = extraction.status
            elif (
                document.processing_status == MedicalDocument.ProcessingStatus.FAILED
                or not hasattr(document, "document_text")
            ):
                status = "FAILED"
            else:
                status = "QUEUED"
            return {
                "document_uuid": document.uuid,
                "document_type": document.document_type,
                "content_kind": "LAB",
                "status": status,
                "sections": [],
            }
        if not hasattr(document, "document_text"):
            status = (
                "FAILED"
                if document.processing_status == MedicalDocument.ProcessingStatus.FAILED
                else "QUEUED"
            )
            return {
                "document_uuid": document.uuid,
                "document_type": document.document_type,
                "content_kind": "NONE",
                "status": status,
                "sections": [],
            }
        sections = extract_narrative(document)
        return {
            "document_uuid": document.uuid,
            "document_type": document.document_type,
            "content_kind": "NARRATIVE",
            "status": "COMPLETED",
            "sections": ExtractedContentSectionSerializer(sections, many=True).data,
        }


@extend_schema_view(
    get=extend_schema(
        operation_id="minor_medical_document_extracted_content"
    ),
)
class MinorExtractedContentView(ExtractedContentView):
    """Guardian-scoped extracted content for an authorized minor's document."""

    def get_patient(self, request, **kwargs):
        return authorized_minor_patient(request, kwargs["minor_uuid"])
