from django.http import FileResponse
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.serializers import ErrorEnvelopeSerializer
from claims.exceptions import AccountClaimNotFound
from claims.models import PatientAccountClaim
from claims.serializers import (
    AccountClaimReviewSerializer,
    AccountClaimSubmissionSerializer,
    ClaimActivationMessageSerializer,
    ClaimActivationSerializer,
    ClaimApprovalSerializer,
    ClaimDecisionSerializer,
    ClaimReceiptSerializer,
    ClaimStatusFilterSerializer,
)
from claims.services.activation import activate_claimed_account
from claims.services.review import (
    approve_account_claim,
    get_claim,
    require_agent,
    transition_claim,
)
from claims.services.submission import submit_account_claim
from claims.throttles import AccountClaimActivationThrottle, AccountClaimSubmitThrottle


def envelope(name, child):
    return inline_serializer(name=name, fields={"data": child})


def paginated_envelope(name, child):
    return envelope(
        name,
        inline_serializer(
            name=f"{name}Page",
            fields={
                "count": serializers.IntegerField(),
                "next": serializers.URLField(allow_null=True),
                "previous": serializers.URLField(allow_null=True),
                "results": child,
            },
        ),
    )


class AccountClaimSubmissionView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    parser_classes = (MultiPartParser, FormParser)
    throttle_classes = [AccountClaimSubmitThrottle]
    throttle_scope = "account_claim_submit"

    @extend_schema(
        operation_id="account_claim_submit",
        request=AccountClaimSubmissionSerializer,
        responses={
            202: envelope("AccountClaimReceipt", ClaimReceiptSerializer()),
            400: ErrorEnvelopeSerializer,
            429: ErrorEnvelopeSerializer,
        },
        tags=["Account claims"],
    )
    def post(self, request):
        serializer = AccountClaimSubmissionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt = submit_account_claim(serializer.validated_data)
        return Response(
            {"data": {"claim_id": receipt.claim_id, "status": receipt.status}},
            status=status.HTTP_202_ACCEPTED,
        )


class AccountClaimVerificationCollectionView(APIView):
    @extend_schema(
        operation_id="account_claim_verification_list",
        parameters=[
            OpenApiParameter(
                "status",
                str,
                enum=list(PatientAccountClaim.Status.values),
                required=False,
            )
        ],
        responses={
            200: paginated_envelope(
                "AccountClaimQueue", AccountClaimReviewSerializer(many=True)
            ),
            400: ErrorEnvelopeSerializer,
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
        },
        tags=["Account claim verification"],
    )
    def get(self, request):
        require_agent(request.user)
        filters = ClaimStatusFilterSerializer(data=request.query_params)
        filters.is_valid(raise_exception=True)
        queryset = PatientAccountClaim.objects.select_related(
            "patient"
        ).prefetch_related(
            "identity_evidence",
            "patient__identity_documents",
            "patient__guardian_relationships",
        )
        if value := filters.validated_data.get("status"):
            queryset = queryset.filter(status=value)
        paginator = PageNumberPagination()
        paginator.page_size = 20
        page = paginator.paginate_queryset(queryset, request)
        return Response(
            {
                "data": {
                    "count": paginator.page.paginator.count,
                    "next": paginator.get_next_link(),
                    "previous": paginator.get_previous_link(),
                    "results": AccountClaimReviewSerializer(page, many=True).data,
                }
            }
        )


class AccountClaimVerificationDetailView(APIView):
    @extend_schema(
        operation_id="account_claim_verification_retrieve",
        responses={
            200: envelope("AccountClaimDetail", AccountClaimReviewSerializer()),
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
        },
        tags=["Account claim verification"],
    )
    def get(self, request, claim_uuid):
        return Response(
            {
                "data": AccountClaimReviewSerializer(
                    get_claim(request.user, claim_uuid)
                ).data
            }
        )


class AccountClaimApproveView(APIView):
    @extend_schema(
        operation_id="account_claim_approve",
        request=None,
        responses={
            200: envelope("AccountClaimApproved", ClaimApprovalSerializer()),
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
            409: ErrorEnvelopeSerializer,
        },
        tags=["Account claim verification"],
    )
    def post(self, request, claim_uuid):
        result = approve_account_claim(
            claim=get_claim(request.user, claim_uuid), agent=request.user
        )
        return Response(
            {
                "data": {
                    "claim_id": result.claim_id,
                    "user_id": result.user_id,
                    "status": result.status,
                    "activation_token": result.activation_token,
                    "activation_expires_at": result.activation_expires_at,
                }
            }
        )


class AccountClaimDecisionView(APIView):
    target_status = None

    @extend_schema(
        request=ClaimDecisionSerializer,
        responses={
            200: envelope("AccountClaimDecision", AccountClaimReviewSerializer()),
            400: ErrorEnvelopeSerializer,
            401: ErrorEnvelopeSerializer,
            403: ErrorEnvelopeSerializer,
            404: ErrorEnvelopeSerializer,
            409: ErrorEnvelopeSerializer,
        },
        tags=["Account claim verification"],
    )
    def post(self, request, claim_uuid):
        serializer = ClaimDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        claim = transition_claim(
            claim=get_claim(request.user, claim_uuid),
            agent=request.user,
            status=self.target_status,
            reason=serializer.validated_data["reason"],
        )
        return Response({"data": AccountClaimReviewSerializer(claim).data})


class AccountClaimRejectView(AccountClaimDecisionView):
    target_status = PatientAccountClaim.Status.REJECTED


class AccountClaimMoreInformationView(AccountClaimDecisionView):
    target_status = PatientAccountClaim.Status.MORE_INFORMATION_REQUIRED


class AccountClaimEvidenceImageView(APIView):
    @extend_schema(
        operation_id="account_claim_evidence_image_retrieve",
        responses={(200, "image/jpeg"): bytes, (200, "image/png"): bytes},
        tags=["Account claim verification"],
    )
    def get(self, request, claim_uuid, evidence_uuid, side):
        claim = get_claim(request.user, claim_uuid)
        evidence = claim.identity_evidence.filter(uuid=evidence_uuid).first()
        if evidence is None or side not in {"front", "back"}:
            raise AccountClaimNotFound()
        identity_file = evidence.front_image if side == "front" else evidence.back_image
        if identity_file is None:
            raise AccountClaimNotFound()
        extension = ".png" if identity_file.media_type == "image/png" else ".jpg"
        return FileResponse(
            identity_file.file.open("rb"),
            content_type=identity_file.media_type,
            as_attachment=True,
            filename=f"claim-evidence-{side}{extension}",
        )


class ClaimedAccountActivationView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [AccountClaimActivationThrottle]
    throttle_scope = "account_claim_activation"

    @extend_schema(
        operation_id="claimed_account_activate",
        request=ClaimActivationSerializer,
        responses={
            200: envelope("ClaimActivationSuccess", ClaimActivationMessageSerializer()),
            400: ErrorEnvelopeSerializer,
            429: ErrorEnvelopeSerializer,
        },
        tags=["Authentication"],
    )
    def post(self, request):
        serializer = ClaimActivationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        activate_claimed_account(**serializer.validated_data)
        return Response({"data": {"message": "Account activated."}})
