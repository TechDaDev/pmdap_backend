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
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from identities.exceptions import (
    IdentityTransitionConflict,
    VerificationAgentRequired,
)
from identities.models import IdentityDocument
from identities.permissions import can_verify_identity
from identities.services import approve_identity_document, reject_identity_document
from opsconsole.context import admin_context

logger = logging.getLogger(__name__)

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


def _redirect_after_action(document):
    nxt = _next_pending_after(document)
    if nxt is not None:
        return redirect("admin:ops_verification_review", document_uuid=nxt.pk)
    return redirect("admin:ops_verification_queue")


@require_GET
def verification_queue(request):
    if not _can_view_verification(request.user):
        raise PermissionDenied
    documents = list(_pending_queryset())
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
        IdentityDocument.objects.select_related("patient", "replaces", "front_image", "back_image"),
        pk=document_uuid,
    )
    return render(
        request,
        "admin/ops/verification_review.html",
        admin_context(
            request,
            document=document,
            patient=document.patient,
            can_mutate=_can_mutate_verification(request.user),
        ),
    )


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
                request, f"Could not verify this document: {getattr(exc, 'code', 'error')}"
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
                request, f"Could not reject this document: {getattr(exc, 'code', 'error')}"
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
        raise Http404("No %s image for this document" % side)
    try:
        handle = identity_file.file.open("rb")
    except FileNotFoundError:
        raise Http404("Image file is missing")
    response = FileResponse(handle, content_type=identity_file.media_type or "application/octet-stream")
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
