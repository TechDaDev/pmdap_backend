from django.http import FileResponse
from drf_spectacular.utils import extend_schema, extend_schema_view, inline_serializer
from rest_framework import serializers, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.serializers import ErrorEnvelopeSerializer
from documents.date_services import confirm_document_date, pending_confirmation_queryset
from documents.page_services import pending_page_units
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
    MedicalDocumentDetailSerializer,
    MedicalDocumentMetadataSerializer,
    MedicalDocumentPageDateConfirmationSerializer,
    MedicalDocumentPageDetailSerializer,
    MedicalDocumentPageSummarySerializer,
    MedicalDocumentSerializer,
    MedicalDocumentUploadSerializer,
    PendingDateConfirmationSerializer,
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
    """Report-unit-aware date-confirmation queue.

    One entry per pending PAGE UNIT. A single-page document contributes one
    entry (unchanged UX); a multi-page PDF contributes up to N entries — one
    per page — each with its own candidates. Queue and its count derive from
    the SAME domain rule so Home badge and the queue page can never drift.
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
        queryset = pending_page_units(patient)
        results = []
        for page_unit in queryset:
            document = page_unit.document
            rows = (
                document.date_candidates.filter(
                    page_number=page_unit.page_number,
                    is_current=True,
                )
                .order_by("-score", "uuid")
            )
            # Canonical candidate shape (serializer field names) — never raw
            # OCR context. Empty list for a zero-candidate page, which is
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
                    "page_number": page_unit.page_number,
                    "page_count": document.pages.count(),
                    "report_subtype": page_unit.report_subtype,
                    "processing_status": page_unit.processing_status,
                    "created_at": document.created_at,
                    "detected_candidates": candidates,
                    "requires_manual_date": not candidates,
                }
            )
        return Response({"data": {"count": len(results), "results": results}})


class _PageAccessMixin:
    """Owner-scoped page access helpers (404 for other patients)."""

    def get_patient(self, request, **kwargs):
        del kwargs
        return owned_profile(request.user)

    def get_document(self, request, document_uuid, **kwargs):
        return active_document(self.get_patient(request, **kwargs), document_uuid)

    def get_page(self, document, page_number):
        page = document.pages.filter(page_number=page_number).first()
        if page is None:
            from documents.exceptions import MedicalDocumentPageNotFound

            raise MedicalDocumentPageNotFound()
        return page


def _page_lab_result_count(document, page_unit):
    extraction = (
        page_unit.lab_extractions.filter(status="COMPLETED")
        .order_by("-created_at")
        .first()
    )
    if extraction is not None:
        return extraction.result_count
    if document.pages.count() == 1:
        extraction = (
            LabReportExtraction.objects.filter(
                document=document, status="COMPLETED"
            )
            .order_by("-created_at")
            .first()
        )
        if extraction is not None:
            return extraction.result_count
    return 0


class MedicalDocumentPageListView(_PageAccessMixin, APIView):
    """Report-unit summary for one owned document.

    One entry per page (page 1 for images). No OCR body, no geometry, no raw
    values — only per-page status/lifecycle metadata.
    """

    @extend_schema(
        operation_id="medical_document_pages",
        responses={
            200: envelope(
                "MedicalDocumentPages",
                MedicalDocumentPageSummarySerializer,
            ),
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
        },
        tags=["Medical documents"],
    )
    def get(self, request, document_uuid, **kwargs):
        del kwargs
        document = self.get_document(request, document_uuid)
        pages = list(document.pages.order_by("page_number"))
        items = [
            {
                "page_number": page.page_number,
                "report_subtype": page.report_subtype,
                "processing_status": page.processing_status,
                "document_date": page.document_date,
                "date_verified": page.date_verified,
                "lab_result_count": _page_lab_result_count(document, page),
                "date_candidate_count": (
                    document.date_candidates.filter(
                        page_number=page.page_number, is_current=True
                    ).count()
                ),
            }
            for page in pages
        ]
        payload = {
            "document_uuid": document.uuid,
            "page_count": len(pages),
            "pages": items,
        }
        return Response(
            {"data": MedicalDocumentPageSummarySerializer(payload).data}
        )


class MedicalDocumentPageDetailView(_PageAccessMixin, APIView):
    """One report page unit with its own candidates + structured results."""

    @extend_schema(
        operation_id="medical_document_page_detail",
        responses={
            200: envelope(
                "MedicalDocumentPageDetail",
                MedicalDocumentPageDetailSerializer,
            ),
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
        },
        tags=["Medical documents"],
    )
    def get(self, request, document_uuid, page_number, **kwargs):
        del kwargs
        document = self.get_document(request, document_uuid)
        page = self.get_page(document, page_number)
        payload = self._payload(document, page)
        return Response({"data": MedicalDocumentPageDetailSerializer(payload).data})

    @staticmethod
    def _payload(document, page):
        from documents.serializers import DateCandidateSerializer
        from labs.serializers import LabResultSerializer

        candidates = DateCandidateSerializer(
            document.date_candidates.filter(
                page_number=page.page_number, is_current=True
            ),
            many=True,
        ).data
        extraction = (
            page.lab_extractions.order_by("-created_at").first()
        )
        results = []
        if extraction is not None and extraction.status == "COMPLETED":
            results = LabResultSerializer(
                extraction.results.order_by("row_index"), many=True
            ).data
        return {
            "document_uuid": document.uuid,
            "page_number": page.page_number,
            "page_count": document.pages.count(),
            "report_subtype": page.report_subtype,
            "processing_status": page.processing_status,
            "processing_failure_code": page.processing_failure_code,
            "document_date": page.document_date,
            "date_verified": page.date_verified,
            "date_source": page.date_source,
            "lab_result_count": _page_lab_result_count(document, page),
            "detected_candidates": candidates,
            "lab_results": results,
        }


class MedicalDocumentPageLabResultsView(_PageAccessMixin, APIView):
    """Structured lab results for ONE report page (owner-only)."""

    @extend_schema(
        operation_id="medical_document_page_lab_results",
        responses={
            200: envelope(
                "MedicalDocumentPageLabResults",
                inline_serializer(
                    name="MedicalDocumentPageLabResultsData",
                    fields={
                        "document_uuid": serializers.UUIDField(),
                        "page_number": serializers.IntegerField(),
                        "extraction_status": serializers.CharField(),
                        "result_count": serializers.IntegerField(),
                        "results": serializers.ListField(
                            child=serializers.DictField()
                        ),
                    },
                ),
            ),
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
        },
        tags=["Medical documents"],
    )
    def get(self, request, document_uuid, page_number, **kwargs):
        del kwargs
        document = self.get_document(request, document_uuid)
        page = self.get_page(document, page_number)
        from labs.serializers import LabResultSerializer

        extraction = (
            page.lab_extractions.order_by("-created_at").first()
        )
        if extraction is None:
            if document.pages.count() == 1:
                extraction = (
                    LabReportExtraction.objects.filter(document=document)
                    .order_by("-created_at")
                    .first()
                )
        if extraction is None:
            status_value = (
                LabReportExtraction.Status.NOT_APPLICABLE
                if document.document_type != MedicalDocument.DocumentType.LABORATORY
                else LabReportExtraction.Status.QUEUED
            )
            return Response(
                {
                    "data": {
                        "document_uuid": document.uuid,
                        "page_number": page.page_number,
                        "extraction_status": status_value,
                        "result_count": 0,
                        "results": [],
                    }
                }
            )
        results = (
            extraction.results.order_by("row_index")
            if extraction.status == LabReportExtraction.Status.COMPLETED
            else extraction.results.none()
        )
        return Response(
            {
                "data": {
                    "document_uuid": document.uuid,
                    "page_number": page.page_number,
                    "extraction_status": extraction.status,
                    "result_count": extraction.result_count,
                    "results": LabResultSerializer(results, many=True).data,
                }
            }
        )


class MedicalDocumentPageDateConfirmationView(_PageAccessMixin, APIView):
    """Confirm (or manually set) the report date for ONE page unit."""

    @extend_schema(
        operation_id="medical_document_page_date_confirm",
        request=MedicalDocumentPageDateConfirmationSerializer,
        responses={
            200: envelope(
                "MedicalDocumentPageDateConfirmation",
                MedicalDocumentPageDetailSerializer,
            ),
            400: ErrorEnvelopeSerializer,
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
            409: ErrorEnvelopeSerializer,
        },
        tags=["Medical documents"],
    )
    def post(self, request, document_uuid, page_number, **kwargs):
        del kwargs
        document = self.get_document(request, document_uuid)
        page = self.get_page(document, page_number)
        from documents.page_services import confirm_page_date

        serializer = MedicalDocumentPageDateConfirmationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        confirm_page_date(
            page_unit=page,
            actor=request.user,
            candidate_id=serializer.validated_data.get("candidate_id"),
            manual_date=serializer.validated_data.get("date"),
        )
        document.refresh_from_db()
        page.refresh_from_db()
        payload = MedicalDocumentPageDetailView._payload(document, page)
        return Response({"data": MedicalDocumentPageDetailSerializer(payload).data})


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
