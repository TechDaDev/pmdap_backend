from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.serializers import ErrorEnvelopeSerializer
from facilities.exceptions import HealthcareFacilityNotFound
from facilities.models import HealthcareFacility
from facilities.normalization import normalize_reference_name
from facilities.serializers import (
    FacilityFilterSerializer,
    HealthcareFacilitySerializer,
)


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


class HealthcareFacilityCollectionView(APIView):
    @extend_schema(
        operation_id="healthcare_facility_list",
        parameters=[FacilityFilterSerializer],
        responses={
            200: paginated_envelope(
                "HealthcareFacilityList", HealthcareFacilitySerializer(many=True)
            ),
            400: ErrorEnvelopeSerializer,
            401: ErrorEnvelopeSerializer,
        },
        tags=["Healthcare facilities"],
    )
    def get(self, request):
        filters = FacilityFilterSerializer(data=request.query_params)
        filters.is_valid(raise_exception=True)
        values = filters.validated_data
        queryset = HealthcareFacility.objects.select_related(
            "country", "region", "city"
        ).prefetch_related("aliases")
        queryset = queryset.filter(active=values["active"])
        if country := values.get("country"):
            queryset = queryset.filter(country_id=country)
        if region := values.get("region"):
            queryset = queryset.filter(
                region__normalized_name=normalize_reference_name(region)
            )
        if city := values.get("city"):
            queryset = queryset.filter(
                city__normalized_name=normalize_reference_name(city)
            )
        if facility_type := values.get("type"):
            queryset = queryset.filter(facility_type=facility_type)
        paginator = PageNumberPagination()
        paginator.page_size = 20
        page = paginator.paginate_queryset(queryset, request)
        return Response(
            {
                "data": {
                    "count": paginator.page.paginator.count,
                    "next": paginator.get_next_link(),
                    "previous": paginator.get_previous_link(),
                    "results": HealthcareFacilitySerializer(page, many=True).data,
                }
            }
        )


class HealthcareFacilityDetailView(APIView):
    @extend_schema(
        operation_id="healthcare_facility_retrieve",
        responses={
            200: envelope(
                "HealthcareFacilityDetail",
                HealthcareFacilitySerializer(read_only=True),
            ),
            401: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
        },
        tags=["Healthcare facilities"],
    )
    def get(self, request, facility_uuid):
        try:
            facility = (
                HealthcareFacility.objects.select_related("country", "region", "city")
                .prefetch_related("aliases")
                .get(uuid=facility_uuid, active=True)
            )
        except (HealthcareFacility.DoesNotExist, ValueError) as exc:
            raise HealthcareFacilityNotFound() from exc
        return Response({"data": HealthcareFacilitySerializer(facility).data})
