import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from audit.models import AuditLog
from audit.services import record_audit
from documents.exceptions import (
    DateCandidateNotFound,
    DateCandidateStale,
    InvalidDateConfirmation,
    InvalidDateConfirmationState,
    InvalidDocumentDate,
    MedicalDocumentNotFound,
)
from documents.models import DocumentDateEvent, MedicalDocument
from processing.models import DateCandidate

logger = logging.getLogger(__name__)

ALLOWED_DATE_DECISION_STATES = {
    MedicalDocument.ProcessingStatus.AWAITING_CONFIRMATION,
    MedicalDocument.ProcessingStatus.DATE_CONFIRMED,
    MedicalDocument.ProcessingStatus.DATE_DETECTED,
    MedicalDocument.ProcessingStatus.DATE_NOT_FOUND,
    MedicalDocument.ProcessingStatus.FAILED,
}


def document_needs_date_confirmation(document):
    """Authoritative domain rule: does this document sit in the confirm queue?

    TRUE when the document is active, reached AWAITING_CONFIRMATION, and the
    report date is not yet user-confirmed.

    Deliberately does NOT depend on DateCandidate rows — OCR may legitimately
    find no date, and such documents still require (manual) confirmation.
    """
    return (
        document.archive_status == MedicalDocument.ArchiveStatus.ACTIVE
        and document.processing_status
        == MedicalDocument.ProcessingStatus.AWAITING_CONFIRMATION
        and not document.date_verified
    )


def pending_confirmation_queryset(patient):
    """Documents currently awaiting date confirmation for `patient`.

    Single source of truth for BOTH the confirm-dates queue and its count, so
    the Home badge and the queue page can never drift.
    """
    return MedicalDocument.objects.filter(
        patient=patient,
        archive_status=MedicalDocument.ArchiveStatus.ACTIVE,
        processing_status=MedicalDocument.ProcessingStatus.AWAITING_CONFIRMATION,
        date_verified=False,
    )


def confirm_document_date(*, document, actor, candidate_id=None, manual_date=None):
    if (candidate_id is None) == (manual_date is None):
        raise InvalidDateConfirmation()
    if manual_date is not None and manual_date > timezone.localdate():
        raise InvalidDocumentDate()
    with transaction.atomic():
        try:
            locked = MedicalDocument.objects.select_for_update().get(
                pk=document.pk,
                archive_status=MedicalDocument.ArchiveStatus.ACTIVE,
            )
        except MedicalDocument.DoesNotExist as exc:
            raise MedicalDocumentNotFound() from exc

        if locked.processing_status not in ALLOWED_DATE_DECISION_STATES:
            raise InvalidDateConfirmationState()

        candidate = None
        if candidate_id is not None:
            candidate = (
                DateCandidate.objects.select_for_update()
                .filter(uuid=candidate_id)
                .first()
            )
            if candidate is None or candidate.document_id != locked.pk:
                raise DateCandidateNotFound()
            if (
                not candidate.is_current
                or candidate.pipeline_version != settings.DATE_PIPELINE_VERSION
            ):
                raise DateCandidateStale()
            new_date = candidate.detected_date
            source = MedicalDocument.DateSource.USER_CONFIRMED
        else:
            new_date = manual_date
            source = MedicalDocument.DateSource.USER_CORRECTED

        latest = locked.date_events.order_by("-created_at", "-uuid").first()
        same_decision = (
            locked.date_verified
            and locked.document_date == new_date
            and locked.date_source == source
            and latest is not None
            and latest.new_date == new_date
            and latest.source == source
            and latest.candidate_id == (candidate.pk if candidate else None)
        )
        if same_decision:
            return locked

        previous_date = locked.document_date
        previously_verified = locked.date_verified
        verified_at = timezone.now()
        locked.document_date = new_date
        locked.date_source = source
        locked.date_verified = True
        locked.date_verified_at = verified_at
        locked.processing_status = MedicalDocument.ProcessingStatus.DATE_CONFIRMED
        locked.processing_failure_code = ""
        locked.processing_started_at = None
        locked.save(
            update_fields=(
                "document_date",
                "date_source",
                "date_verified",
                "date_verified_at",
                "processing_status",
                "processing_failure_code",
                "processing_started_at",
                "updated_at",
            )
        )
        if source == MedicalDocument.DateSource.USER_CORRECTED:
            action = DocumentDateEvent.Action.DATE_CORRECTED
        elif previously_verified:
            action = DocumentDateEvent.Action.DATE_RECONFIRMED
        else:
            action = DocumentDateEvent.Action.DATE_CONFIRMED
        event = DocumentDateEvent.objects.create(
            document=locked,
            actor=actor,
            action=action,
            previous_date=previous_date,
            new_date=new_date,
            source=source,
            candidate=candidate,
        )
        record_audit(
            action=(
                AuditLog.Action.DATE_CORRECTED
                if source == MedicalDocument.DateSource.USER_CORRECTED
                else AuditLog.Action.DATE_CONFIRMED
            ),
            actor=actor,
            patient=locked.patient,
            resource_type="MEDICAL_DOCUMENT",
            resource_uuid=locked.uuid,
            previous_values={
                "document_date": previous_date.isoformat() if previous_date else None
            },
            new_values={"document_date": new_date.isoformat()},
            metadata={"date_source": source},
        )
    logger.info(
        "Document date decision persisted",
        extra={
            "document_uuid": str(locked.uuid),
            "action": action,
            "source": source,
            "event_uuid": str(event.uuid),
        },
    )
    return locked
