from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from guardians.exceptions import GuardianRelationshipConflict
from guardians.models import GuardianRelationship
from guardians.services import (
    approve_guardian_relationship,
    reject_guardian_relationship,
)
from identities.permissions import can_verify_identity
from opsconsole.context import admin_context


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
    adult = relationship.guardian_user.patient_profile
    minor = relationship.minor_patient
    return render(
        request,
        "admin/ops/guardian_review.html",
        admin_context(
            request,
            relationship=relationship,
            adult_confirmed=adult.identity_status == adult.IdentityStatus.VERIFIED,
            minor_confirmed=minor.identity_status == minor.IdentityStatus.VERIFIED,
            age_valid=minor.is_minor,
        ),
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
        return redirect("admin:ops_guardian_review", relationship_uuid=relationship.pk)


@require_POST
def guardian_reject(request, relationship_uuid):
    _require_reviewer(request)
    reason = (request.POST.get("reason") or "").strip()
    relationship = get_object_or_404(GuardianRelationship, pk=relationship_uuid)
    if not reason:
        messages.error(request, "Rejection reason is required.")
        return redirect("admin:ops_guardian_review", relationship_uuid=relationship.pk)
    reject_guardian_relationship(
        relationship=relationship, agent=request.user, reason=reason
    )
    messages.success(request, "Guardian relationship rejected.")
    return redirect("admin:ops_guardian_queue")
