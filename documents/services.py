import hashlib
import logging
import uuid

from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from audit.models import AuditLog
from audit.services import record_audit
from documents.exceptions import (
    DuplicateMedicalDocument,
    MedicalDocumentNotFound,
    MedicalFileStorageFailed,
)
from documents.models import MedicalDocument, MedicalDocumentEvent, StoredFile
from documents.scanning import default_file_security_scanner
from documents.validation import inspect_medical_upload
from facilities.exceptions import (
    HealthcareFacilityInactive,
    HealthcareFacilityNotFound,
)
from facilities.models import HealthcareFacility

EDITABLE_METADATA_FIELDS = (
    "document_type",
    "title",
    "description",
    "facility_name",
    "location_text",
    "department",
    "physician_name",
)


def _classification_source(patient, actor):
    if patient.user_id == actor.pk:
        return MedicalDocument.ClassificationSource.USER_SELECTED
    return MedicalDocument.ClassificationSource.GUARDIAN_SELECTED


def _active_facility(facility_uuid, *, for_update=False):
    if facility_uuid is None:
        return None
    queryset = HealthcareFacility.objects
    if for_update:
        queryset = queryset.select_for_update()
    try:
        facility = queryset.get(uuid=facility_uuid)
    except (HealthcareFacility.DoesNotExist, ValueError) as exc:
        raise HealthcareFacilityNotFound() from exc
    if not facility.active:
        raise HealthcareFacilityInactive()
    return facility


logger = logging.getLogger(__name__)


def _record_event(document, event_type, actor, metadata=None):
    return MedicalDocumentEvent.objects.create(
        document=document,
        event_type=event_type,
        actor=actor,
        metadata=metadata or {},
    )


def _record_duplicate(document, actor):
    _record_event(
        document,
        MedicalDocumentEvent.EventType.DUPLICATE_REJECTED,
        actor,
    )


def _stored_digest(stored_file):
    digest = hashlib.sha256()
    with stored_file.file.open("rb") as original:
        for chunk in iter(lambda: original.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_medical_document(*, patient, actor, upload, metadata, scanner=None):
    validated = inspect_medical_upload(upload)
    existing = MedicalDocument.objects.filter(
        patient=patient,
        content_sha256=validated.sha256,
        archive_status=MedicalDocument.ArchiveStatus.ACTIVE,
    ).first()
    if existing is not None:
        _record_duplicate(existing, actor)
        raise DuplicateMedicalDocument(existing.uuid)

    scan_result = (scanner or default_file_security_scanner).scan(validated.content)
    values = {key: metadata[key] for key in EDITABLE_METADATA_FIELDS if key in metadata}
    if "document_date" in metadata:
        values["document_date"] = metadata["document_date"]
    values["classification_source"] = _classification_source(patient, actor)
    if values.get("document_date") is not None:
        values.update(
            date_source=MedicalDocument.DateSource.USER_ENTERED,
            date_verified=True,
            date_verified_at=timezone.now(),
        )

    storage_name = f"medical/{uuid.uuid4().hex}{validated.extension}"
    stored = StoredFile(
        original_filename=validated.original_filename,
        mime_type=validated.mime_type,
        size_bytes=len(validated.content),
        sha256=validated.sha256,
        page_count=validated.page_count,
        malware_scan_status=scan_result.status,
    )
    persisted_name = ""
    try:
        with transaction.atomic():
            facility = _active_facility(
                metadata.get("healthcare_facility_id"), for_update=True
            )
            stored.file.save(
                storage_name,
                ContentFile(validated.content),
                save=False,
            )
            persisted_name = stored.file.name
            stored.save(force_insert=True)
            if _stored_digest(stored) != validated.sha256:
                raise OSError("Stored medical file failed integrity verification.")
            stored.integrity_status = StoredFile.IntegrityStatus.VALID
            stored.save(update_fields=("integrity_status", "updated_at"))
            is_pdf = validated.mime_type == "application/pdf"
            document = MedicalDocument.objects.create(
                patient=patient,
                uploaded_by=actor,
                stored_file=stored,
                content_sha256=validated.sha256,
                processing_status=(
                    MedicalDocument.ProcessingStatus.QUEUED
                    if is_pdf
                    else MedicalDocument.ProcessingStatus.UPLOADED
                ),
                healthcare_facility=facility,
                **values,
            )
            _record_event(
                document,
                MedicalDocumentEvent.EventType.UPLOADED,
                actor,
                {"malware_scan_status": scan_result.status},
            )
            record_audit(
                action=AuditLog.Action.DOCUMENT_UPLOADED,
                actor=actor,
                patient=patient,
                resource_type="MEDICAL_DOCUMENT",
                resource_uuid=document.uuid,
                new_values={
                    "document_type": document.document_type,
                    "classification_source": document.classification_source,
                    "processing_status": document.processing_status,
                },
                metadata={
                    "stored_file": str(stored.uuid),
                    "malware_scan_status": scan_result.status,
                },
            )
            if is_pdf:
                _record_event(
                    document,
                    MedicalDocumentEvent.EventType.PDF_EXTRACTION_QUEUED,
                    actor,
                )
                transaction.on_commit(
                    lambda document_uuid=str(document.uuid): _enqueue_pdf_extraction(
                        document_uuid
                    )
                )
            else:
                from processing.ocr_services import schedule_ocr

                schedule_ocr(document, record_event=False)
        return document
    except IntegrityError as exc:
        if persisted_name:
            stored.file.storage.delete(persisted_name)
        existing = MedicalDocument.objects.filter(
            patient=patient,
            content_sha256=validated.sha256,
            archive_status=MedicalDocument.ArchiveStatus.ACTIVE,
        ).first()
        if existing is not None:
            _record_duplicate(existing, actor)
            raise DuplicateMedicalDocument(existing.uuid) from exc
        raise
    except OSError as exc:
        stored.file.storage.delete(persisted_name or storage_name)
        raise MedicalFileStorageFailed() from exc
    except Exception:
        stored.file.storage.delete(persisted_name or storage_name)
        raise


def _enqueue_pdf_extraction(document_uuid):
    from processing.tasks import extract_pdf_text

    try:
        extract_pdf_text.delay(document_uuid)
    except Exception:
        logger.error(
            "PDF extraction enqueue failed",
            extra={"document_uuid": document_uuid},
        )


def verify_stored_file_integrity(stored_file, *, actor=None):
    with transaction.atomic():
        locked = StoredFile.objects.select_for_update().get(pk=stored_file.pk)
        try:
            digest = _stored_digest(locked)
            actual_size = locked.file.size
        except (OSError, ValueError):
            locked.integrity_status = StoredFile.IntegrityStatus.MISSING
            locked.save(update_fields=("integrity_status", "updated_at"))
            _record_integrity_evidence(locked, locked.integrity_status, actor)
            return locked
        if (actual_size is not None and actual_size != locked.size_bytes) or (
            digest != locked.sha256
        ):
            locked.integrity_status = StoredFile.IntegrityStatus.CORRUPTED
        else:
            locked.integrity_status = StoredFile.IntegrityStatus.VALID
        locked.save(update_fields=("integrity_status", "updated_at"))
        _record_integrity_evidence(locked, locked.integrity_status, actor)
        return locked


def _record_integrity_evidence(stored_file, status, actor):
    try:
        document = stored_file.medical_document
    except MedicalDocument.DoesNotExist:
        return
    action = (
        AuditLog.Action.INTEGRITY_FAILURE
        if status
        in {
            StoredFile.IntegrityStatus.CORRUPTED,
            StoredFile.IntegrityStatus.MISSING,
        }
        else AuditLog.Action.FILE_INTEGRITY_CHECKED
    )
    _record_event(
        document,
        MedicalDocumentEvent.EventType.FILE_INTEGRITY_CHECKED,
        actor,
        {"integrity_status": status},
    )
    record_audit(
        action=action,
        actor=actor,
        patient=document.patient,
        resource_type="MEDICAL_DOCUMENT",
        resource_uuid=document.uuid,
        new_values={"integrity_status": status},
        metadata={"stored_file": str(stored_file.uuid)},
    )


def update_medical_document(*, document, actor, metadata):
    with transaction.atomic():
        try:
            locked = MedicalDocument.objects.select_for_update().get(
                pk=document.pk,
                archive_status=MedicalDocument.ArchiveStatus.ACTIVE,
            )
        except MedicalDocument.DoesNotExist as exc:
            raise MedicalDocumentNotFound() from exc
        changed_fields = []
        events = []
        old_type = locked.document_type
        for field in EDITABLE_METADATA_FIELDS:
            if field in metadata and getattr(locked, field) != metadata[field]:
                setattr(locked, field, metadata[field])
                changed_fields.append(field)
        if "document_type" in changed_fields:
            locked.classification_source = _classification_source(locked.patient, actor)
            changed_fields.append("classification_source")
            events.append(
                (
                    MedicalDocumentEvent.EventType.DOCUMENT_TYPE_CHANGED,
                    {
                        "old_type": old_type,
                        "new_type": locked.document_type,
                        "classification_source": locked.classification_source,
                    },
                )
            )
        if "healthcare_facility_id" in metadata:
            facility = _active_facility(
                metadata["healthcare_facility_id"], for_update=True
            )
            if locked.healthcare_facility_id != (facility.pk if facility else None):
                old_facility_id = locked.healthcare_facility_id
                locked.healthcare_facility = facility
                changed_fields.append("healthcare_facility")
                events.append(
                    (
                        MedicalDocumentEvent.EventType.DOCUMENT_FACILITY_CHANGED,
                        {
                            "old_facility_id": str(old_facility_id)
                            if old_facility_id
                            else None,
                            "new_facility_id": str(facility.pk) if facility else None,
                        },
                    )
                )
        event_fields = {
            MedicalDocumentEvent.EventType.DOCUMENT_LOCATION_UPDATED: {
                "facility_name",
                "location_text",
            },
            MedicalDocumentEvent.EventType.DOCUMENT_DEPARTMENT_UPDATED: {"department"},
            MedicalDocumentEvent.EventType.DOCUMENT_PHYSICIAN_METADATA_UPDATED: {
                "physician_name"
            },
        }
        for event_type, fields in event_fields.items():
            present = sorted(fields.intersection(changed_fields))
            if present:
                events.append((event_type, {"fields": present}))
        if not changed_fields:
            return locked
        locked.save(update_fields=(*changed_fields, "updated_at"))
        generic = sorted({"title", "description"}.intersection(changed_fields))
        if generic:
            events.append(
                (
                    MedicalDocumentEvent.EventType.METADATA_UPDATED,
                    {"fields": generic},
                )
            )
        for event_type, event_metadata in events:
            _record_event(locked, event_type, actor, event_metadata)
        if "document_type" in changed_fields:
            record_audit(
                action=AuditLog.Action.DOCUMENT_TYPE_CHANGED,
                actor=actor,
                patient=locked.patient,
                resource_type="MEDICAL_DOCUMENT",
                resource_uuid=locked.uuid,
                previous_values={"document_type": old_type},
                new_values={"document_type": locked.document_type},
            )
        if "healthcare_facility" in changed_fields:
            record_audit(
                action=AuditLog.Action.DOCUMENT_FACILITY_CHANGED,
                actor=actor,
                patient=locked.patient,
                resource_type="MEDICAL_DOCUMENT",
                resource_uuid=locked.uuid,
                previous_values={
                    "healthcare_facility": str(old_facility_id)
                    if old_facility_id
                    else None
                },
                new_values={
                    "healthcare_facility": str(locked.healthcare_facility_id)
                    if locked.healthcare_facility_id
                    else None
                },
            )
        generic_changed = sorted({"title", "description"}.intersection(changed_fields))
        if generic_changed:
            record_audit(
                action=AuditLog.Action.DOCUMENT_METADATA_UPDATED,
                actor=actor,
                patient=locked.patient,
                resource_type="MEDICAL_DOCUMENT",
                resource_uuid=locked.uuid,
                metadata={"fields": generic_changed},
            )
    return locked


def soft_delete_medical_document(*, document, actor):
    with transaction.atomic():
        try:
            locked = MedicalDocument.objects.select_for_update().get(
                pk=document.pk,
                archive_status=MedicalDocument.ArchiveStatus.ACTIVE,
            )
        except MedicalDocument.DoesNotExist as exc:
            raise MedicalDocumentNotFound() from exc
        locked.archive_status = MedicalDocument.ArchiveStatus.DELETED
        locked.deleted_at = timezone.now()
        locked.deleted_by = actor
        locked.save(
            update_fields=(
                "archive_status",
                "deleted_at",
                "deleted_by",
                "updated_at",
            )
        )
        _record_event(
            locked,
            MedicalDocumentEvent.EventType.DELETED,
            actor,
        )
        record_audit(
            action=AuditLog.Action.DOCUMENT_DELETED,
            actor=actor,
            patient=locked.patient,
            resource_type="MEDICAL_DOCUMENT",
            resource_uuid=locked.uuid,
        )
    return locked


def purge_medical_document(*, document):
    """Hard-delete a document and ALL its children (page units, extractions,
    candidates, OCR text, events). Used by test/ops cleanup — not the normal
    user lifecycle (which is soft-delete)."""
    from documents.models import DocumentDateEvent, MedicalDocumentEvent
    from labs.models import LabReportExtraction
    from processing.models import DateCandidate

    with transaction.atomic():
        DocumentDateEvent.objects.filter(document=document).delete()
        DateCandidate.objects.filter(document=document).delete()
        MedicalDocumentEvent.objects.filter(document=document).delete()
        LabReportExtraction.objects.filter(document=document).delete()
        if hasattr(document, "document_text"):
            document.document_text.delete()
        document.pages.all().delete()
        stored = document.stored_file
        document.delete()
        if stored:
            stored.delete()
    return None