import logging
import time

from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.serializers import ErrorEnvelopeSerializer
from archive.search_serializers import SearchFilterSerializer
from archive.search_services import MedicalDocumentSearchService
from archive.serializers import ArchiveDocumentSerializer
from archive.throttling import MedicalSearchThrottle
from guardians.api import authorized_minor_relationship
from patients.api import owned_profile

logger = logging.getLogger(__name__)


def search_page_envelope(name):
    page = inline_serializer(
        name=f"{name}Page",
        fields={
            "count": serializers.IntegerField(),
            "next": serializers.URLField(allow_null=True),
            "previous": serializers.URLField(allow_null=True),
            "results": ArchiveDocumentSerializer(many=True),
        },
    )
    return inline_serializer(name=name, fields={"data": page})


def search_response(request, patient):
    started = time.monotonic()
    filters = SearchFilterSerializer(data=request.query_params)
    filters.is_valid(raise_exception=True)
    values = filters.validated_data
    service = MedicalDocumentSearchService(patient)
    queryset = service.search_queryset(values)
    keyword = values.get("q")
    if keyword:
        queryset = service.with_keyword(queryset, keyword)
    queryset = service.order(queryset, values, has_keyword=bool(keyword))
    paginator = PageNumberPagination()
    paginator.page_size = 20
    page = paginator.paginate_queryset(queryset, request)
    count = paginator.page.paginator.count
    logger.info(
        "Medical search served",
        extra={
            "patient_uuid": str(patient.uuid),
            "query_present": bool(keyword),
            "filters": sorted(values),
            "result_count": count,
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
    )
    return Response(
        {
            "data": {
                "count": count,
                "next": paginator.get_next_link(),
                "previous": paginator.get_previous_link(),
                "results": ArchiveDocumentSerializer(page, many=True).data,
            }
        }
    )


class SearchView(APIView):
    def get_throttles(self):
        return [MedicalSearchThrottle()]

    @extend_schema(
        operation_id="medical_search",
        parameters=[SearchFilterSerializer],
        responses={
            200: search_page_envelope("SearchResults"),
            400: ErrorEnvelopeSerializer,
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            429: ErrorEnvelopeSerializer,
        },
        tags=["Search"],
    )
    def get(self, request):
        patient = owned_profile(request.user)
        return search_response(request, patient)


class MinorSearchView(APIView):
    def get_throttles(self):
        return [MedicalSearchThrottle()]

    @extend_schema(
        operation_id="minor_medical_search",
        parameters=[SearchFilterSerializer],
        responses={
            200: search_page_envelope("MinorSearchResults"),
            400: ErrorEnvelopeSerializer,
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
            429: ErrorEnvelopeSerializer,
        },
        tags=["Search"],
    )
    def get(self, request, minor_uuid):
        relationship = authorized_minor_relationship(request.user, minor_uuid)
        return search_response(request, relationship.minor_patient)
