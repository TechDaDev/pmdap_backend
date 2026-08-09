import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

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
