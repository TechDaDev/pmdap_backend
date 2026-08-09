import logging
import time

from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.serializers import ErrorEnvelopeSerializer
from archive.serializers import (
    ArchiveDocumentSerializer,
    ArchiveFilterSerializer,
    ArchiveSummarySerializer,
)
from archive.services import (
    UNCONFIRMED_ORDERING,
    VERIFIED_ORDERING,
    ArchiveQueryService,
)
from guardians.api import authorized_minor_relationship
from patients.api import owned_profile

logger = logging.getLogger(__name__)


def envelope(name, child):
    return inline_serializer(name=name, fields={"data": child})


def archive_page_envelope(name):
    page = inline_serializer(
        name=f"{name}Page",
        fields={
            "count": serializers.IntegerField(),
            "next": serializers.URLField(allow_null=True),
            "previous": serializers.URLField(allow_null=True),
            "results": ArchiveDocumentSerializer(many=True),
            "unconfirmed_date_count": serializers.IntegerField(),
        },
    )
    return envelope(name, page)


def archive_list_response(request, patient):
    started = time.monotonic()
    filters = ArchiveFilterSerializer(data=request.query_params)
    filters.is_valid(raise_exception=True)
    values = filters.validated_data
    service = ArchiveQueryService(patient)
    if values.get("date_status") == "UNCONFIRMED":
        queryset = service.unconfirmed_queryset(values).order_by(*UNCONFIRMED_ORDERING)
    else:
        queryset = service.chronological_queryset(values).order_by(*VERIFIED_ORDERING)
    paginator = PageNumberPagination()
    paginator.page_size = 20
    page = paginator.paginate_queryset(queryset, request)
    count = paginator.page.paginator.count
    unconfirmed_count = service.unconfirmed_count()
    logger.info(
        "Archive list served",
        extra={
            "patient_uuid": str(patient.uuid),
            "filters": sorted(values),
            "result_count": count,
            "unconfirmed_date_count": unconfirmed_count,
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
                "unconfirmed_date_count": unconfirmed_count,
            }
        }
    )


def summary_response(request, patient):
    started = time.monotonic()
    summary = ArchiveQueryService(patient).summary()
    logger.info(
        "Archive summary served",
        extra={
            "patient_uuid": str(patient.uuid),
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
    )
    del request
    return Response({"data": summary})


class ArchiveView(APIView):
    @extend_schema(
        operation_id="archive_list",
        parameters=[ArchiveFilterSerializer],
        responses={
            200: archive_page_envelope("ArchiveList"),
            400: ErrorEnvelopeSerializer,
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
        },
        tags=["Archive"],
    )
    def get(self, request):
        patient = owned_profile(request.user)
        return archive_list_response(request, patient)


class ArchiveSummaryView(APIView):
    @extend_schema(
        operation_id="archive_summary",
        responses={
            200: envelope("ArchiveSummaryResponse", ArchiveSummarySerializer()),
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
        },
        tags=["Archive"],
    )
    def get(self, request):
        patient = owned_profile(request.user)
        return summary_response(request, patient)


class MinorArchiveView(APIView):
    @extend_schema(
        operation_id="minor_archive_list",
        parameters=[ArchiveFilterSerializer],
        responses={
            200: archive_page_envelope("MinorArchiveList"),
            400: ErrorEnvelopeSerializer,
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
        },
        tags=["Archive"],
    )
    def get(self, request, minor_uuid):
        relationship = authorized_minor_relationship(request.user, minor_uuid)
        return archive_list_response(request, relationship.minor_patient)


class MinorArchiveSummaryView(APIView):
    @extend_schema(
        operation_id="minor_archive_summary",
        responses={
            200: envelope("MinorArchiveSummaryResponse", ArchiveSummarySerializer()),
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
        },
        tags=["Archive"],
    )
    def get(self, request, minor_uuid):
        relationship = authorized_minor_relationship(request.user, minor_uuid)
        return summary_response(request, relationship.minor_patient)
