"""Domain-level page/report-unit orchestration for MedicalDocumentPage.

One uploaded PDF stays one archived MedicalDocument. Each page is an
independent report unit: own OCR state, date candidates, confirmation state
and structured lab extraction. This module owns the deterministic parent
aggregation rule and page-scoped date confirmation.
"""
import logging
import re

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from audit.models import AuditLog
from audit.services import record_audit
from documents.exceptions import (
    DateCandidateNotFound,
    DateCandidateStale,
    InvalidDateConfirmation,
    InvalidDocumentDate,
    MedicalDocumentNotFound,
)
from documents.models import (
    DocumentDateEvent,
    MedicalDocument,
    MedicalDocumentPage,
)
from processing.models import DateCandidate

logger = logging.getLogger(__name__)

NON_TERMINAL_PAGE_STATUSES = {
    MedicalDocumentPage.ProcessingStatus.QUEUED,
    MedicalDocumentPage.ProcessingStatus.OCR_PROCESSING,
    MedicalDocumentPage.ProcessingStatus.EXTRACTING,
}

PAGE_ALLOWED_DATE_DECISION_STATES = {
    MedicalDocumentPage.ProcessingStatus.AWAITING_CONFIRMATION,
    MedicalDocumentPage.ProcessingStatus.READY,
}

# Strong specialized cues win over generic chemistry: a shared "chemistry"
# header must NOT dominate a CBC or hormones page. Chemistry requires explicit
# analyte words, not the bare word "chemistry".
SUBTYPE_CUES = (
    (
        MedicalDocumentPage.ReportSubtype.LAB_CBC,
        (
            "cbc", "complete blood", "hematolog", "haematolog", "blood count",
            "wbc", "rbc", "hgb", "hct", "mcv", "mch", "mchc", "plt", "platelet",
            "packed cell",
        ),
    ),
    (
        MedicalDocumentPage.ReportSubtype.LAB_HORMONES,
        (
            "hormone", "vitamin d", "vitamin b", "vitamin", "thyroid", "tsh",
            "t3", "t4", "prolactin", "estradiol", "testosterone", "cortisol",
            "fsh", "lh",
        ),
    ),
    (
        MedicalDocumentPage.ReportSubtype.RADIOLOGY,
        (
            "radiology", "x-ray", "xray", "x ray", "ct scan", "mri",
            "ultrasound", "sonograph", "echocardiograph", "mammograph",
        ),
    ),
    (
        MedicalDocumentPage.ReportSubtype.LAB_CHEMISTRY,
        (
            "biochem", "lipid profile", "cholesterol", "triglyceride", "hdl",
            "ldl", "glucose", "creatinine", "urea", "bilirubin", "uric acid",
            "sgpt", "sgot", "gpt", "got", "alkaline phosphatase", "albumin",
            "total protein",
        ),
    ),
)


def detect_report_subtype(text: str) -> str:
    """Conservative layout/category classification from generic page cues.

    Layout metadata only — never clinical interpretation. Falls back to
    NARRATIVE when no lab table cue is present, UNKNOWN otherwise.
    """
    lowered = (text or "").lower()
    for subtype, cues in SUBTYPE_CUES:
        for cue in cues:
            if re.search(r"(^|[^a-z])" + re.escape(cue) + r"([^a-z]|$)", lowered):
                return subtype
    # Generic lab table cue (header row) -> lab page of unknown subtype.
    if re.search(r"(^|\n)[^|\n]*(result|value|reference|test)[^|\n]*\n", lowered):
        return MedicalDocumentPage.ReportSubtype.LAB_CHEMISTRY
    if _looks_like_narrative(lowered):
        return MedicalDocumentPage.ReportSubtype.NARRATIVE
    return MedicalDocumentPage.ReportSubtype.UNKNOWN


def _looks_like_narrative(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    return len(lines) >= 6 and all(len(line) > 30 for line in lines)


def ensure_page_units(document, *, source_pages=None):
    """Create/refresh MedicalDocumentPage units for a document (idempotent).

    ``source_pages`` optional iterable of page numbers (1..N). Defaults to
    DocumentTextPage rows, falling back to a single page 1 unit.
    """
    if source_pages is None:
        if hasattr(document, "document_text"):
            source_pages = [
                page.page_number
                for page in document.document_text.pages.order_by("page_number")
            ]
        else:
            source_pages = [1]
    source_pages = sorted(set(source_pages))
    existing = {
        page.page_number: page
        for page in document.pages.filter(page_number__in=source_pages)
    }
    created = []
    for number in source_pages:
        page = existing.get(number)
        if page is None:
            page = MedicalDocumentPage.objects.create(
                document=document,
                page_number=number,
                processing_status=MedicalDocumentPage.ProcessingStatus.QUEUED,
            )
            created.append(page)
    return created


def sync_document_to_page_units(document):
    """Mirror document-level state onto its single page-1 unit.

    Backward-compat bridge for the document-level date/lab pipelines so a
    single-page document's page unit stays consistent with the page-based
    confirm queue. No-op for multi-page documents (page pipeline owns them).
    """
    if document.pages.count() != 1:
        if not document.pages.exists():
            ensure_page_units(document, source_pages=[1])
        if document.pages.count() != 1:
            return
    unit = document.pages.first()
    unit.document_date = document.document_date
    unit.date_verified = document.date_verified
    unit.date_source = document.date_source
    unit.date_verified_at = document.date_verified_at
    if document.processing_status == MedicalDocument.ProcessingStatus.DATE_CONFIRMED:
        unit.processing_status = MedicalDocumentPage.ProcessingStatus.READY
    elif document.processing_status in {
        MedicalDocument.ProcessingStatus.AWAITING_CONFIRMATION,
        MedicalDocument.ProcessingStatus.DATE_DETECTED,
        MedicalDocument.ProcessingStatus.DATE_NOT_FOUND,
        MedicalDocument.ProcessingStatus.PARTIAL,
    }:
        unit.processing_status = (
            MedicalDocumentPage.ProcessingStatus.AWAITING_CONFIRMATION
        )
    elif document.processing_status == MedicalDocument.ProcessingStatus.FAILED:
        unit.processing_status = MedicalDocumentPage.ProcessingStatus.FAILED
    elif document.processing_status == MedicalDocument.ProcessingStatus.TEXT_EXTRACTED:
        unit.processing_status = MedicalDocumentPage.ProcessingStatus.EXTRACTING
    unit.save(
        update_fields=(
            "document_date",
            "date_verified",
            "date_source",
            "date_verified_at",
            "processing_status",
            "updated_at",
        )
    )


def page_lab_result_count(page_unit) -> int:
    return page_unit.lab_extractions.filter(
        status="COMPLETED"
    ).first().result_count if page_unit.lab_extractions.filter(
        status="COMPLETED"
    ).exists() else 0


def _finalize_page(page_unit):
    """Advance one page unit to its next stable state.

    Page is terminal when BOTH its date detection and its lab extraction (when
    applicable) have finished. A lab parse failure fails only that page.
    """
    document = page_unit.document
    lab = (
        page_unit.lab_extractions.order_by("-created_at").first()
    )
    lab_applicable = document.document_type == MedicalDocument.DocumentType.LABORATORY
    lab_terminal = lab is None and not lab_applicable
    if lab is not None:
        lab_terminal = lab.status in {
            "COMPLETED",
            "NOT_APPLICABLE",
            "FAILED",
        }
    if lab is not None and lab.status == "FAILED":
        page_unit.processing_status = MedicalDocumentPage.ProcessingStatus.FAILED
        page_unit.processing_failure_code = lab.error_code or "lab_parse_failed"
    elif not lab_terminal:
        page_unit.processing_status = MedicalDocumentPage.ProcessingStatus.EXTRACTING
    elif page_unit.date_verified:
        page_unit.processing_status = MedicalDocumentPage.ProcessingStatus.READY
    else:
        page_unit.processing_status = (
            MedicalDocumentPage.ProcessingStatus.AWAITING_CONFIRMATION
        )
    page_unit.save(
        update_fields=("processing_status", "processing_failure_code", "updated_at")
    )
    recalculate_document_processing_state(document)
    return page_unit.processing_status


def _apply_parent_date(document, pages):
    """Parent date aggregation rule.

    When all confirmed READY pages share one date -> parent date = that date.
    When confirmed page dates differ -> parent date cleared (mixed state).
    Otherwise the parent date is left untouched (still awaiting confirmation).
    """
    confirmed = {
        page.document_date
        for page in pages
        if page.processing_status == MedicalDocumentPage.ProcessingStatus.READY
        and page.date_verified
        and page.document_date is not None
    }
    if len(confirmed) == 1:
        document.document_date = confirmed.pop()
        document.date_verified = True
        return
    if len(confirmed) > 1:
        document.document_date = None
        document.date_verified = False


def recalculate_document_processing_state(document, *, save=True):
    """Deterministic parent aggregate status from child page units.

    Rule:
    - any page still QUEUED/OCR_PROCESSING/EXTRACTING -> PROCESSING
    - all READY                              -> DATE_CONFIRMED
    - any AWAITING_CONFIRMATION present      -> AWAITING_CONFIRMATION
    - mix of READY + FAILED                  -> PARTIAL
    - all FAILED                             -> FAILED
    - fallback (all terminal, none confirm)  -> AWAITING_CONFIRMATION

    Idempotent, safe to call after any page terminal/state change.
    """
    pages = list(document.pages.all().order_by("page_number"))
    if not pages:
        return document.processing_status

    statuses = {page.processing_status for page in pages}
    if statuses & NON_TERMINAL_PAGE_STATUSES:
        parent = MedicalDocument.ProcessingStatus.PROCESSING
    elif statuses == {MedicalDocumentPage.ProcessingStatus.READY}:
        parent = MedicalDocument.ProcessingStatus.DATE_CONFIRMED
    elif MedicalDocumentPage.ProcessingStatus.AWAITING_CONFIRMATION in statuses:
        parent = MedicalDocument.ProcessingStatus.AWAITING_CONFIRMATION
    elif MedicalDocumentPage.ProcessingStatus.FAILED in statuses:
        if statuses == {MedicalDocumentPage.ProcessingStatus.FAILED}:
            parent = MedicalDocument.ProcessingStatus.FAILED
        else:
            parent = MedicalDocument.ProcessingStatus.PARTIAL
    else:
        parent = MedicalDocument.ProcessingStatus.AWAITING_CONFIRMATION

    _apply_parent_date(document, pages)
    if save:
        document.processing_status = parent
        document.save(
            update_fields=(
                "processing_status",
                "document_date",
                "date_verified",
                "updated_at",
            )
        )
    return parent


def pending_page_units(patient):
    """Page units currently awaiting date confirmation for `patient`.

    Single source of truth for BOTH the confirm queue and its count, so the
    Home badge and the queue page can never drift. One multi-page PDF can
    contribute up to N entries.
    """
    return MedicalDocumentPage.objects.filter(
        document__patient=patient,
        document__archive_status=MedicalDocument.ArchiveStatus.ACTIVE,
        processing_status=MedicalDocumentPage.ProcessingStatus.AWAITING_CONFIRMATION,
        date_verified=False,
    ).select_related("document").order_by(
        "document__created_at", "page_number"
    )


def confirm_page_date(*, page_unit, actor, candidate_id=None, manual_date=None):
    """Confirm (or manually set) the report date for ONE page unit."""
    if (candidate_id is None) == (manual_date is None):
        raise InvalidDateConfirmation()
    if manual_date is not None and manual_date > timezone.localdate():
        raise InvalidDocumentDate()
    with transaction.atomic():
        locked = (
            MedicalDocumentPage.objects.select_for_update()
            .filter(pk=page_unit.pk)
            .select_related("document")
            .first()
        )
        if (
            locked is None
            or locked.document.archive_status != MedicalDocument.ArchiveStatus.ACTIVE
        ):
            raise MedicalDocumentNotFound()
        if locked.processing_status not in PAGE_ALLOWED_DATE_DECISION_STATES:
            from documents.exceptions import InvalidDateConfirmationState

            raise InvalidDateConfirmationState()

        candidate = None
        if candidate_id is not None:
            candidate = (
                DateCandidate.objects.select_for_update()
                .filter(uuid=candidate_id)
                .first()
            )
            if (
                candidate is None
                or candidate.document_id != locked.document_id
                or (
                    candidate.page_unit_id is not None
                    and candidate.page_unit_id != locked.pk
                )
            ):
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

        same_decision = (
            locked.date_verified
            and locked.document_date == new_date
            and locked.date_source == source
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
        locked.processing_status = MedicalDocumentPage.ProcessingStatus.READY
        locked.processing_failure_code = ""
        locked.save(
            update_fields=(
                "document_date",
                "date_source",
                "date_verified",
                "date_verified_at",
                "processing_status",
                "processing_failure_code",
                "updated_at",
            )
        )
        if source == MedicalDocument.DateSource.USER_CORRECTED:
            action = DocumentDateEvent.Action.DATE_CORRECTED
        elif previously_verified:
            action = DocumentDateEvent.Action.DATE_RECONFIRMED
        else:
            action = DocumentDateEvent.Action.DATE_CONFIRMED
        DocumentDateEvent.objects.create(
            document=locked.document,
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
            patient=locked.document.patient,
            resource_type="MEDICAL_DOCUMENT_PAGE",
            resource_uuid=locked.uuid,
            metadata={"document_uuid": str(locked.document.uuid), "page_number": locked.page_number},
            previous_values={
                "document_date": previous_date.isoformat() if previous_date else None
            },
            new_values={"document_date": new_date.isoformat()},
        )
        # Parent aggregate follows the child.
        recalculate_document_processing_state(locked.document)
    logger.info(
        "Page date decision persisted",
        extra={
            "document_uuid": str(locked.document.uuid),
            "page_number": locked.page_number,
            "action": action,
            "source": source,
        },
    )
    return locked
