from drf_spectacular.utils import extend_schema, extend_schema_view, inline_serializer
from rest_framework.response import Response
from rest_framework.views import APIView

from documents.api import active_document, envelope
from documents.models import MedicalDocument
from labs.models import LabReportExtraction, LabResult
from labs.serializers import LabResultSerializer, LabResultsResponseSerializer
from patients.api import owned_profile

# 404 keeps other patients' documents opaque (MedicalDocumentNotFound raised
# by active_document). Verification agents have no patient profile and are
# rejected by owned_profile with 403.


class LabResultsView(APIView):
    """Read-only structured lab results for one owned document."""

    def get_patient(self, request, **kwargs):
        del kwargs
        return owned_profile(request.user)

    def get_document(self, request, document_uuid, **kwargs):
        return active_document(self.get_patient(request, **kwargs), document_uuid)

    @extend_schema(
        operation_id="medical_document_lab_results",
        summary="Structured lab results for one document",
        description=(
            "Read-only, owner-only. Returns structured values extracted from "
            "the patient's own uploaded report. Never returns raw OCR text or "
            "geometry. Synthetic example only."
        ),
        responses={
            200: envelope(
                "LabResultsSuccess",
                LabResultsResponseSerializer(read_only=True),
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
        extraction = (
            LabReportExtraction.objects.filter(document=document)
            .order_by("-created_at")
            .first()
        )
        if extraction is None:
            if document.document_type != MedicalDocument.DocumentType.LABORATORY:
                status = LabReportExtraction.Status.NOT_APPLICABLE
            elif (
                document.processing_status == MedicalDocument.ProcessingStatus.FAILED
                or not hasattr(document, "document_text")
            ):
                status = LabReportExtraction.Status.FAILED
            else:
                status = LabReportExtraction.Status.QUEUED
            return {
                "document_uuid": document.uuid,
                "document_type": document.document_type,
                "extraction_status": status,
                "pipeline_version": None,
                "result_count": 0,
                "results": [],
            }
        results = (
            LabResult.objects.filter(extraction=extraction)
            .order_by("page_number", "row_index")
            if extraction.status == LabReportExtraction.Status.COMPLETED
            else LabResult.objects.none()
        )
        return {
            "document_uuid": document.uuid,
            "document_type": document.document_type,
            "extraction_status": extraction.status,
            "pipeline_version": extraction.pipeline_version,
            "result_count": extraction.result_count,
            "results": LabResultSerializer(results, many=True).data,
        }


@extend_schema_view(
    get=extend_schema(operation_id="minor_medical_document_lab_results")
)
class MinorLabResultsView(LabResultsView):
    """Guardian-scoped lab results for an authorized minor's document."""

    def get_patient(self, request, **kwargs):
        from documents.api import authorized_minor_patient

        return authorized_minor_patient(request, kwargs["minor_uuid"])
