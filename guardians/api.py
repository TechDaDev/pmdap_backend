from django.http import FileResponse
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from accounts.serializers import ErrorEnvelopeSerializer
from guardians.exceptions import (
    GuardianRelationshipNotFound,
    IdempotencyKeyRequired,
    InvalidIdempotencyKey,
)
from guardians.models import GuardianEvidence, GuardianRelationship
from guardians.serializers import (
    EmptySerializer,
    GuardianRelationshipFilterSerializer,
    GuardianRelationshipVerificationSerializer,
    MinorCreateResponseSerializer,
    MinorCreateSerializer,
    MinorSerializer,
    RelationshipRejectionSerializer,
)
from guardians.services import (
    approve_guardian_relationship,
    create_minor,
    eligible_guardian_profile,
    reject_guardian_relationship,
)
from identities.exceptions import VerificationAgentRequired


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


def page_response(request, items, serializer_class):
    paginator = PageNumberPagination()
    paginator.page_size = 20
    page = paginator.paginate_queryset(items, request)
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


def require_agent(user):
    if user.role != User.Role.IDENTITY_VERIFICATION_AGENT:
        raise VerificationAgentRequired()


def verification_relationship(user, relationship_uuid):
    require_agent(user)
    try:
        return (
            GuardianRelationship.objects.select_related(
                "minor_patient", "guardian_user__patient_profile"
            )
            .prefetch_related(
                "evidences",
                "minor_patient__identity_documents",
                "guardian_user__patient_profile__identity_documents",
            )
            .get(uuid=relationship_uuid)
        )
    except (GuardianRelationship.DoesNotExist, ValueError) as exc:
        raise GuardianRelationshipNotFound() from exc


def authorized_minor_relationship(user, minor_uuid):
    try:
        relationship = (
            GuardianRelationship.objects.select_related("minor_patient")
            .filter(
                guardian_user=user,
                minor_patient__uuid=minor_uuid,
                verification_status=GuardianRelationship.VerificationStatus.VERIFIED,
                active=True,
            )
            .first()
        )
    except ValueError as exc:
        raise GuardianRelationshipNotFound() from exc
    if relationship is None:
        raise GuardianRelationshipNotFound()
    if not relationship.minor_patient.is_minor:
        raise GuardianRelationshipNotFound()
    eligible_guardian_profile(user)
    return relationship


class MinorCollectionView(APIView):
    parser_classes = (MultiPartParser, FormParser)

    @extend_schema(
        operation_id="minor_list",
        responses={
            200: paginated_envelope("MinorList", MinorSerializer(many=True)),
            401: ErrorEnvelopeSerializer,
        },
        tags=["Minors and guardians"],
    )
    def get(self, request):
        eligible_guardian_profile(request.user)
        relationships = GuardianRelationship.objects.filter(
            guardian_user=request.user,
            verification_status__in=(
                GuardianRelationship.VerificationStatus.PENDING,
                GuardianRelationship.VerificationStatus.VERIFIED,
            ),
        ).select_related("minor_patient")
        minors = []
        seen_minor_ids = set()
        for relationship in relationships:
            if (
                relationship.minor_patient.is_minor
                and (
                    relationship.verification_status
                    == GuardianRelationship.VerificationStatus.PENDING
                    or relationship.active
                )
                and relationship.minor_patient_id not in seen_minor_ids
            ):
                relationship.minor_patient.authorized_relationship = relationship
                minors.append(relationship.minor_patient)
                seen_minor_ids.add(relationship.minor_patient_id)
        return page_response(request, minors, MinorSerializer)

    @extend_schema(
        operation_id="minor_create",
        request=MinorCreateSerializer,
        parameters=[
            OpenApiParameter(
                name="Idempotency-Key",
                type=str,
                location=OpenApiParameter.HEADER,
                required=True,
            )
        ],
        responses={
            200: envelope(
                "MinorCreateReplay", MinorCreateResponseSerializer(read_only=True)
            ),
            201: envelope(
                "MinorCreated", MinorCreateResponseSerializer(read_only=True)
            ),
            400: ErrorEnvelopeSerializer,
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            409: ErrorEnvelopeSerializer,
        },
        tags=["Minors and guardians"],
    )
    def post(self, request):
        key = request.headers.get("Idempotency-Key")
        if key is None:
            raise IdempotencyKeyRequired()
        if not key.strip() or len(key) > 128:
            raise InvalidIdempotencyKey()
        serializer = MinorCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = create_minor(
            guardian=request.user,
            idempotency_key=key,
            validated_data=serializer.validated_data,
        )
        data = MinorCreateResponseSerializer(
            result.minor, context={"relationship": result.relationship}
        ).data
        return Response(
            {"data": data},
            status=status.HTTP_201_CREATED if result.created else status.HTTP_200_OK,
        )


class MinorDetailView(APIView):
    @extend_schema(
        operation_id="minor_retrieve",
        responses={
            200: envelope("MinorDetail", MinorSerializer(read_only=True)),
            401: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
        },
        tags=["Minors and guardians"],
    )
    def get(self, request, minor_uuid):
        relationship = authorized_minor_relationship(request.user, minor_uuid)
        relationship.minor_patient.authorized_relationship = relationship
        return Response({"data": MinorSerializer(relationship.minor_patient).data})


class GuardianVerificationCollectionView(APIView):
    @extend_schema(
        operation_id="guardian_relationship_verification_list",
        parameters=[
            OpenApiParameter(
                name="status",
                type=str,
                enum=list(GuardianRelationship.VerificationStatus.values),
                required=False,
            )
        ],
        responses={
            200: paginated_envelope(
                "GuardianRelationshipQueue",
                GuardianRelationshipVerificationSerializer(many=True),
            ),
            400: ErrorEnvelopeSerializer,
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
        },
        tags=["Guardian relationship verification"],
    )
    def get(self, request):
        require_agent(request.user)
        filters = GuardianRelationshipFilterSerializer(data=request.query_params)
        filters.is_valid(raise_exception=True)
        queryset = GuardianRelationship.objects.select_related(
            "minor_patient", "guardian_user__patient_profile"
        ).prefetch_related(
            "evidences",
            "minor_patient__identity_documents",
            "guardian_user__patient_profile__identity_documents",
        )
        if value := filters.validated_data.get("status"):
            queryset = queryset.filter(verification_status=value)
        return page_response(
            request, queryset, GuardianRelationshipVerificationSerializer
        )


class GuardianVerificationDetailView(APIView):
    @extend_schema(
        operation_id="guardian_relationship_verification_retrieve",
        responses={
            200: envelope(
                "GuardianRelationshipVerificationDetail",
                GuardianRelationshipVerificationSerializer(read_only=True),
            ),
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
        },
        tags=["Guardian relationship verification"],
    )
    def get(self, request, relationship_uuid):
        relationship = verification_relationship(request.user, relationship_uuid)
        return Response(
            {"data": GuardianRelationshipVerificationSerializer(relationship).data}
        )


class GuardianVerificationApproveView(APIView):
    @extend_schema(
        operation_id="guardian_relationship_verification_approve",
        request=EmptySerializer,
        responses={
            200: envelope(
                "GuardianRelationshipApproved",
                GuardianRelationshipVerificationSerializer(read_only=True),
            ),
            400: ErrorEnvelopeSerializer,
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
            409: ErrorEnvelopeSerializer,
        },
        tags=["Guardian relationship verification"],
    )
    def post(self, request, relationship_uuid):
        relationship = verification_relationship(request.user, relationship_uuid)
        serializer = EmptySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        relationship = approve_guardian_relationship(
            relationship=relationship, agent=request.user
        )
        return Response(
            {"data": GuardianRelationshipVerificationSerializer(relationship).data}
        )


class GuardianVerificationRejectView(APIView):
    @extend_schema(
        operation_id="guardian_relationship_verification_reject",
        request=RelationshipRejectionSerializer,
        responses={
            200: envelope(
                "GuardianRelationshipRejected",
                GuardianRelationshipVerificationSerializer(read_only=True),
            ),
            400: ErrorEnvelopeSerializer,
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
            409: ErrorEnvelopeSerializer,
        },
        tags=["Guardian relationship verification"],
    )
    def post(self, request, relationship_uuid):
        relationship = verification_relationship(request.user, relationship_uuid)
        serializer = RelationshipRejectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        relationship = reject_guardian_relationship(
            relationship=relationship,
            agent=request.user,
            reason=serializer.validated_data["rejection_reason"],
        )
        return Response(
            {"data": GuardianRelationshipVerificationSerializer(relationship).data}
        )


class GuardianEvidenceFileView(APIView):
    @extend_schema(
        operation_id="guardian_relationship_evidence_file",
        responses={
            (200, "image/jpeg"): bytes,
            (200, "image/png"): bytes,
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
        },
        tags=["Guardian relationship verification"],
    )
    def get(self, request, relationship_uuid, evidence_uuid):
        relationship = verification_relationship(request.user, relationship_uuid)
        try:
            evidence = GuardianEvidence.objects.select_related("file").get(
                uuid=evidence_uuid, relationship=relationship
            )
        except (GuardianEvidence.DoesNotExist, ValueError) as exc:
            raise GuardianRelationshipNotFound() from exc
        handle = evidence.file.file.open("rb")
        extension = ".png" if evidence.file.media_type == "image/png" else ".jpg"
        return FileResponse(
            handle,
            content_type=evidence.file.media_type,
            as_attachment=True,
            filename=f"guardian-evidence{extension}",
        )
