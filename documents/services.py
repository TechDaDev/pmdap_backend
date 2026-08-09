import hashlib
import logging
import uuid

from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from documents.exceptions import (
    DuplicateMedicalDocument,
    MedicalDocumentNotFound,
    MedicalFileStorageFailed,
)
from documents.models import MedicalDocument, MedicalDocumentEvent, StoredFile
from documents.scanning import default_file_security_scanner
from documents.validation import inspect_medical_upload

EDITABLE_METADATA_FIELDS = (
    "document_type",
    "title",
    "description",
    "document_date",
    "facility_name",
    "location_text",
    "department",
    "physician_name",
)

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
        raise DuplicateMedicalDocument()

    scan_result = (scanner or default_file_security_scanner).scan(validated.content)
    values = {key: metadata[key] for key in EDITABLE_METADATA_FIELDS if key in metadata}
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
                **values,
            )
            _record_event(
                document,
                MedicalDocumentEvent.EventType.UPLOADED,
                actor,
                {"malware_scan_status": scan_result.status},
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
            raise DuplicateMedicalDocument() from exc
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
    status = (
        StoredFile.IntegrityStatus.VALID
        if _stored_digest(stored_file) == stored_file.sha256
        else StoredFile.IntegrityStatus.CORRUPTED
    )
    stored_file.integrity_status = status
    stored_file.save(update_fields=("integrity_status", "updated_at"))
    try:
        document = stored_file.medical_document
    except MedicalDocument.DoesNotExist:
        return stored_file
    _record_event(
        document,
        MedicalDocumentEvent.EventType.FILE_INTEGRITY_CHECKED,
        actor,
        {"integrity_status": status},
    )
    return stored_file


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
        for field in EDITABLE_METADATA_FIELDS:
            if field in metadata:
                setattr(locked, field, metadata[field])
                changed_fields.append(field)
        if "document_date" in metadata:
            if metadata["document_date"] is None:
                locked.date_source = ""
                locked.date_verified = False
                locked.date_verified_at = None
            else:
                locked.date_source = MedicalDocument.DateSource.USER_CORRECTED
                locked.date_verified = True
                locked.date_verified_at = timezone.now()
            changed_fields.extend(("date_source", "date_verified", "date_verified_at"))
        if not changed_fields:
            return locked
        locked.save(update_fields=(*changed_fields, "updated_at"))
        _record_event(
            locked,
            MedicalDocumentEvent.EventType.METADATA_UPDATED,
            actor,
            {"fields": sorted(set(changed_fields))},
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
    return locked
