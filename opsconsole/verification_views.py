"""Identity verification workstation views (staff-only admin pages).

Authorization model
--------------------
* Viewing the queue, review page, and identity images requires ``is_staff``
  (enforced by the admin site) AND (superuser OR role == IDENTITY_VERIFICATION_AGENT).
* Approve/reject mutations additionally require role == IDENTITY_VERIFICATION_AGENT,
  which mirrors the enforcement inside ``identities.services`` (defense in depth).
  Superusers may view for oversight but cannot mutate without the agent role.

Privacy
-------
No document/national/family/passport numbers are rendered on the queue page.
Sensitive fields only appear on the single-document review page. Images are
streamed through authenticated, no-store endpoints.
"""

import logging

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_POST

from identities.corrections import (
    REVIEWABLE_FIELDS,
    correct_verified_identity,
    update_identity_review_fields,
)
from identities.exceptions import (
    IdentityCorrectionConflict,
    IdentityTransitionConflict,
    StaleReviewConflict,
    VerificationAgentRequired,
)
from identities.models import IdentityDocument, IdentityFieldCorrection
from identities.permissions import can_verify_identity
from identities.services import approve_identity_document, reject_identity_document
from opsconsole.context import admin_context
from patients.models import PatientProfile

logger = logging.getLogger(__name__)

FIELD_LABELS = {
    "given_name": "First name",
    "father_name": "Father's name",
    "grandfather_name": "Grandfather's name",
    "mother_name": "Mother's name",
    "date_of_birth": "Date of birth",
    "sex": "Sex",
    "blood_group": "Blood group",
    "nationality": "Nationality",
    "document_number": "Document number",
    "national_number": "National number",
    "family_number": "Family number",
    "unique_card_body_number": "Card-body number",
}

_PENDING = IdentityDocument.VerificationStatus.PENDING
_CURRENT = IdentityDocument.LifecycleStatus.CURRENT


def _can_view_verification(user):
    return can_verify_identity(user)


def _can_mutate_verification(user):
    return can_verify_identity(user)


def _pending_queryset():
    return (
        IdentityDocument.objects.filter(
            verification_status=_PENDING,
            status=_CURRENT,
        )
        .select_related("patient", "replaces")
        .order_by("created_at", "uuid")
    )


def _next_pending_after(document):
    qs = _pending_queryset().exclude(pk=document.pk)
    after = qs.filter(created_at__gt=document.created_at).first()
    return after or qs.first()


def _review_fields(document):
    """Per-field review state for the workstation template. Structured values
    only (never raw OCR text); numbers rendered on the private page only."""
    from identities.corrections import _as_text

    profile = document.patient
    out = []
    for field in sorted(REVIEWABLE_FIELDS):
        from identities.corrections import PROFILE_FIELDS

        source = profile if field in PROFILE_FIELDS else document
        original = _as_text(getattr(source, field))
        staged = getattr(document, f"reviewed_{field}", None)
        reviewed = _as_text(staged) if staged else original
        out.append(
            {
                "field": field,
                "label": FIELD_LABELS.get(field, field),
                "original": original,
                "reviewed": reviewed,
                "corrected": reviewed != original,
            }
        )
    return out


def _redirect_after_action(document):
    nxt = _next_pending_after(document)
    if nxt is not None:
        return redirect("admin:ops_verification_review", document_uuid=nxt.pk)
    return redirect("admin:ops_verification_queue")


@require_GET
def verification_queue(request):
    if not _can_view_verification(request.user):
        raise PermissionDenied
    documents = list(_pending_queryset().prefetch_related("field_corrections"))
    return render(
        request,
        "admin/ops/verification_queue.html",
        admin_context(
            request,
            documents=documents,
            count=len(documents),
            can_mutate=_can_mutate_verification(request.user),
        ),
    )


@require_GET
def verification_review(request, document_uuid):
    if not _can_view_verification(request.user):
        raise PermissionDenied
    document = get_object_or_404(
        IdentityDocument.objects.select_related(
            "patient", "replaces", "front_image", "back_image"
        ),
        pk=document_uuid,
    )
    return render(
        request,
        "admin/ops/verification_review.html",
        admin_context(
            request,
            document=document,
            patient=document.patient,
            review_fields=_review_fields(document),
            corrections=list(document.field_corrections.all()),
            can_mutate=_can_mutate_verification(request.user),
            sex_choices=PatientProfile.Sex.choices,
            blood_choices=PatientProfile.BloodGroup.choices,
            reason_categories=IdentityFieldCorrection.ReasonCategory.choices,
        ),
    )


@require_POST
def verification_review_fields(request, document_uuid):
    """Save reviewer corrections for a PENDING identity (staged, no approve)."""
    if not _can_mutate_verification(request.user):
        raise PermissionDenied
    document = get_object_or_404(IdentityDocument, pk=document_uuid)
    try:
        review_version = int(request.POST.get("review_version") or -1)
    except ValueError:
        review_version = -1
    fields = {
        name: request.POST.get(name, "")
        for name in REVIEWABLE_FIELDS
        if name in request.POST
    }
    try:
        update_identity_review_fields(
            actor=request.user,
            document=document,
            corrections=fields,
            review_version=review_version,
        )
        messages.success(request, "Corrections saved. The identity remains pending.")
    except StaleReviewConflict:
        messages.error(request, "This review has changed. Refresh and retry.")
    except IdentityTransitionConflict:
        messages.error(request, "This identity is no longer reviewable.")
    except VerificationAgentRequired:
        raise PermissionDenied from None
    except Exception as exc:
        messages.error(
            request, f"Could not save corrections: {getattr(exc, 'messages', exc)}"
        )
    return redirect("admin:ops_verification_review", document_uuid=document.pk)


@require_POST
def verification_correct_verified(request, document_uuid):
    """Correct a VERIFIED identity. Requires a reason category."""
    if not _can_mutate_verification(request.user):
        raise PermissionDenied
    document = get_object_or_404(IdentityDocument, pk=document_uuid)
    try:
        review_version = int(request.POST.get("review_version") or -1)
    except ValueError:
        review_version = -1
    reason_category = (request.POST.get("reason_category") or "").strip()
    note = (request.POST.get("note") or "").strip()
    fields = {
        name: request.POST.get(name, "")
        for name in REVIEWABLE_FIELDS
        if name in request.POST
    }
    if not reason_category:
        messages.error(request, "A correction reason is required.")
        return redirect("admin:ops_verification_review", document_uuid=document.pk)
    try:
        correct_verified_identity(
            actor=request.user,
            document=document,
            corrections=fields,
            reason_category=reason_category,
            note=note,
            review_version=review_version,
        )
        messages.success(request, "Verified identity corrected.")
    except StaleReviewConflict:
        messages.error(request, "This review has changed. Refresh and retry.")
    except IdentityTransitionConflict:
        messages.error(request, "This identity is not in a correctable state.")
    except IdentityCorrectionConflict as exc:
        messages.error(request, f"{getattr(exc, 'default_detail', 'Conflict')}")
    except VerificationAgentRequired:
        raise PermissionDenied from None
    except Exception as exc:
        messages.error(
            request, f"Could not correct identity: {getattr(exc, 'messages', exc)}"
        )
    return redirect("admin:ops_verification_review", document_uuid=document.pk)


def verification_approve(request, document_uuid):
    """GET renders the confirmation page; POST performs the approval."""
    if not _can_mutate_verification(request.user):
        raise PermissionDenied
    document = get_object_or_404(IdentityDocument, pk=document_uuid)
    if request.method == "POST":
        try:
            approve_identity_document(document=document, agent=request.user)
            messages.success(request, "Identity document verified.")
            return _redirect_after_action(document)
        except (IdentityTransitionConflict, VerificationAgentRequired) as exc:
            messages.error(
                request,
                f"Could not verify this document: {getattr(exc, 'code', 'error')}",
            )
            return redirect("admin:ops_verification_review", document_uuid=document.pk)
    return render(
        request,
        "admin/ops/verification_confirm.html",
        admin_context(request, document=document, patient=document.patient),
    )


def verification_reject(request, document_uuid):
    """GET renders the rejection form; POST performs the rejection."""
    if not _can_mutate_verification(request.user):
        raise PermissionDenied
    document = get_object_or_404(IdentityDocument, pk=document_uuid)
    if request.method == "POST":
        reason = (request.POST.get("reason") or "").strip()
        if not reason:
            return render(
                request,
                "admin/ops/verification_reject.html",
                admin_context(
                    request,
                    document=document,
                    patient=document.patient,
                    reason_required=True,
                ),
            )
        try:
            reject_identity_document(document=document, agent=request.user, reason=reason)
            messages.success(request, "Identity document rejected.")
            return _redirect_after_action(document)
        except (IdentityTransitionConflict, VerificationAgentRequired) as exc:
            messages.error(
                request,
                f"Could not reject this document: {getattr(exc, 'code', 'error')}",
            )
            return redirect("admin:ops_verification_review", document_uuid=document.pk)
    return render(
        request,
        "admin/ops/verification_reject.html",
        admin_context(request, document=document, patient=document.patient),
    )


def _stream_identity_image(request, document_uuid, side):
    if not _can_view_verification(request.user):
        raise PermissionDenied
    document = get_object_or_404(
        IdentityDocument.objects.select_related("front_image", "back_image"),
        pk=document_uuid,
    )
    identity_file = document.front_image if side == "front" else document.back_image
    if identity_file is None:
        raise Http404(f"No {side} image for this document")
    try:
        handle = identity_file.file.open("rb")
    except FileNotFoundError:
        raise Http404("Image file is missing") from None
    response = FileResponse(
        handle,
        content_type=identity_file.media_type or "application/octet-stream",
    )
    response["Cache-Control"] = "private, no-store, max-age=0"
    response["Pragma"] = "no-cache"
    response["X-Robots-Tag"] = "noindex"
    response["Content-Disposition"] = "inline"
    return response


@require_GET
def identity_image_front(request, document_uuid):
    return _stream_identity_image(request, document_uuid, "front")


@require_GET
def identity_image_back(request, document_uuid):
    return _stream_identity_image(request, document_uuid, "back")
