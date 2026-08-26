from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from guardians.exceptions import GuardianRelationshipConflict
from guardians.models import GuardianEvidence, GuardianRelationship
from guardians.services import (
    EVIDENCE_POLICY_VERSION,
    approve_guardian_relationship,
    can_approve_guardian_relationship,
    reject_guardian_relationship,
)
from identities.models import IdentityDocument
from identities.permissions import can_verify_identity
from opsconsole.context import admin_context

REJECTION_CATEGORIES = {
    "Identity not verified",
    "Relationship evidence insufficient",
    "Family evidence mismatch",
    "Incorrect relationship type",
    "Other",
}


def _require_reviewer(request):
    if not can_verify_identity(request.user):
        raise PermissionDenied


def _queue():
    return (
        GuardianRelationship.objects.filter(
            verification_status=GuardianRelationship.VerificationStatus.PENDING,
            ended_at__isnull=True,
        )
        .select_related("minor_patient", "guardian_user__patient_profile")
        .prefetch_related("evidences")
        .order_by("created_at", "uuid")
    )


@require_GET
def guardian_queue(request):
    _require_reviewer(request)
    relationships = list(_queue())
    for relationship in relationships:
        relationship.approval_evaluation = can_approve_guardian_relationship(
            relationship
        )
    return render(
        request,
        "admin/ops/guardian_queue.html",
        admin_context(request, relationships=relationships, count=len(relationships)),
    )


@require_GET
def guardian_review(request, relationship_uuid):
    _require_reviewer(request)
    relationship = get_object_or_404(
        GuardianRelationship.objects.select_related(
            "minor_patient", "guardian_user__patient_profile"
        ).prefetch_related("evidences"),
        pk=relationship_uuid,
    )
    return _render_review(request, relationship)


def _pending_card(profile):
    return (
        IdentityDocument.objects.filter(
            patient=profile,
            document_type=IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD,
            status=IdentityDocument.LifecycleStatus.CURRENT,
            verification_status=IdentityDocument.VerificationStatus.PENDING,
        )
        .order_by("-created_at")
        .first()
    )


def _identity_summary(profile, card):
    return {
        "name": profile.full_name,
        "first_name": profile.given_name,
        "father_name": profile.father_name,
        "grandfather_name": profile.grandfather_name,
        "mother_name": profile.mother_name,
        "sex": profile.get_sex_display(),
        "age_eligible": profile.is_minor,
        "identity_status": profile.get_identity_status_display(),
        "card_state": "Verified / Current" if card else "Pending / Missing",
        "family_number_present": bool(card and card.family_number),
        "national_number_present": bool(card and card.national_number),
        "card_body_number_present": bool(card and card.unique_card_body_number),
        "profile_field_state": "Confirmed",
        "identifier_state": "Verified" if card else "Confirmed",
    }


def _render_review(request, relationship, *, status=200):
    adult = relationship.guardian_user.patient_profile
    minor = relationship.minor_patient
    decision = can_approve_guardian_relationship(
        relationship,
        refresh=relationship.evidence_policy_version != EVIDENCE_POLICY_VERSION,
    )
    adult_pending_card = _pending_card(adult) if not decision.adult_card else None
    minor_pending_card = _pending_card(minor) if not decision.minor_card else None
    return render(
        request,
        "admin/ops/guardian_review.html",
        admin_context(
            request,
            relationship=relationship,
            decision=decision,
            adult=adult,
            minor=minor,
            adult_summary=_identity_summary(adult, decision.adult_card),
            minor_summary=_identity_summary(minor, decision.minor_card),
            adult_card=decision.adult_card,
            minor_card=decision.minor_card,
            adult_pending_card=adult_pending_card,
            minor_pending_card=minor_pending_card,
        ),
        status=status,
    )


@require_POST
def guardian_approve(request, relationship_uuid):
    _require_reviewer(request)
    relationship = get_object_or_404(GuardianRelationship, pk=relationship_uuid)
    try:
        approve_guardian_relationship(relationship=relationship, agent=request.user)
        messages.success(request, "Guardian relationship verified.")
        return redirect("admin:ops_guardian_queue")
    except GuardianRelationshipConflict:
        messages.error(request, "Relationship evidence does not satisfy policy.")
        relationship = get_object_or_404(
            GuardianRelationship.objects.select_related(
                "minor_patient", "guardian_user__patient_profile"
            ).prefetch_related("evidences"),
            pk=relationship.pk,
        )
        return _render_review(request, relationship, status=409)


@require_POST
def guardian_reject(request, relationship_uuid):
    _require_reviewer(request)
    category = (request.POST.get("reason_category") or "").strip()
    explanation = (request.POST.get("explanation") or "").strip()
    reason = (request.POST.get("reason") or "").strip()
    if category:
        reason = ""
        if category in REJECTION_CATEGORIES:
            reason = category if not explanation else f"{category}: {explanation[:500]}"
    relationship = get_object_or_404(GuardianRelationship, pk=relationship_uuid)
    if not reason:
        messages.error(request, "Rejection reason is required.")
        return redirect("admin:ops_guardian_review", relationship_uuid=relationship.pk)
    reject_guardian_relationship(
        relationship=relationship, agent=request.user, reason=reason
    )
    messages.success(request, "Guardian relationship rejected.")
    return redirect("admin:ops_guardian_queue")


@require_GET
def guardian_evidence_file(request, relationship_uuid, evidence_uuid):
    _require_reviewer(request)
    evidence = get_object_or_404(
        GuardianEvidence.objects.select_related("file", "relationship"),
        pk=evidence_uuid,
        relationship_id=relationship_uuid,
    )
    try:
        handle = evidence.file.file.open("rb")
    except FileNotFoundError:
        raise Http404("Evidence file is missing") from None
    response = FileResponse(
        handle, content_type=evidence.file.media_type or "application/octet-stream"
    )
    response["Cache-Control"] = "private, no-store, max-age=0"
    response["Pragma"] = "no-cache"
    response["X-Robots-Tag"] = "noindex"
    response["Content-Disposition"] = "inline"
    return response
