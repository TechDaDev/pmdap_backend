import hashlib
import logging
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.core.files.base import ContentFile
from django.db import IntegrityError
from django.test import override_settings
from django.utils import timezone

from accounts.models import User
from documents.models import MedicalDocument, MedicalDocumentEvent, StoredFile
from patients.models import PatientProfile
from processing.extraction import (
    PDFEncryptedError,
    PDFTextPageResult,
    PDFTextResult,
)
from processing.models import DocumentText, DocumentTextPage
from processing.services import RetryablePDFProcessingError, process_pdf_document
from tests.test_pdf_text_extraction import pdf_bytes

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def use_private_test_storage(settings, tmp_path):
    settings.MEDICAL_FILE_ROOT = tmp_path


def queued_document(
    tmp_path,
    *,
    content=b"synthetic-pdf-content",
    mime_type="application/pdf",
):
    sequence = User.objects.count() + 1
    user = User.objects.create_user(
        email=f"processing-owner-{sequence}@example.com",
        password="A-complex-password-2026!",
        status=User.Status.ACTIVE,
    )
    patient = PatientProfile.objects.create(
        user=user,
        digital_id=f"{sequence:017d}",
        full_name="Synthetic Processing Owner",
        date_of_birth=date(1990, 1, 2),
        sex=PatientProfile.Sex.UNSPECIFIED,
        nationality="IQ",
    )
    digest = hashlib.sha256(content).hexdigest()
    stored = StoredFile(
        original_filename="report.pdf",
        mime_type=mime_type,
        size_bytes=len(content),
        sha256=digest,
        page_count=2,
        integrity_status=StoredFile.IntegrityStatus.VALID,
    )
    stored.file.save("medical/processing.pdf", ContentFile(content), save=False)
    stored.save()
    return MedicalDocument.objects.create(
        patient=patient,
        uploaded_by=user,
        stored_file=stored,
        content_sha256=digest,
        document_type=MedicalDocument.DocumentType.MEDICAL_REPORT,
        processing_status=MedicalDocument.ProcessingStatus.QUEUED,
    )


def extracted_result(*, usable=True, weak_second=False):
    pages = (
        PDFTextPageResult(1, "First digital clinical page", 24, False),
        PDFTextPageResult(
            2,
            "scan" if weak_second else "Second digital clinical page",
            4 if weak_second else 25,
            weak_second,
        ),
    )
    text = "\n\f\n".join(page.text for page in pages)
    return PDFTextResult(
        text=text,
        page_count=2,
        pages=pages,
        character_count=len(text),
        usable=usable,
        reason="usable_pdf_text" if usable else "insufficient_meaningful_text",
        metadata={
            "extraction_method": "PDF_TEXT",
            "extractor_name": "PyMuPDF",
            "extractor_version": "1.28.0",
            "pipeline_version": "m7-v1",
            "meaningful_character_count": sum(
                page.meaningful_character_count for page in pages
            ),
            "pages_requiring_ocr": [
                page.page_number for page in pages if page.requires_ocr
            ],
        },
    )


def extractor_returning(result):
    extractor = Mock()
    extractor.extract.return_value = result
    return extractor


def test_success_persists_canonical_pages_and_preserves_original_evidence(tmp_path):
    document = queued_document(tmp_path)
    original_hash = document.stored_file.sha256

    outcome = process_pdf_document(
        str(document.uuid),
        extractor=extractor_returning(extracted_result(weak_second=True)),
    )

    document.refresh_from_db()
    extracted = document.document_text
    assert outcome == "TEXT_EXTRACTED"
    assert document.processing_status == MedicalDocument.ProcessingStatus.TEXT_EXTRACTED
    assert document.processing_failure_code == ""
    assert extracted.text.startswith("First digital clinical page")
    assert extracted.has_pages_requiring_ocr is True
    assert list(extracted.pages.values_list("page_number", "requires_ocr")) == [
        (1, False),
        (2, True),
    ]
    assert document.stored_file.sha256 == original_hash
    assert document.events.filter(
        event_type=MedicalDocumentEvent.EventType.PDF_EXTRACTION_STARTED
    ).exists()
    assert document.events.filter(
        event_type=MedicalDocumentEvent.EventType.PDF_TEXT_EXTRACTED
    ).exists()


def test_nonusable_valid_pdf_is_ocr_required_and_keeps_page_text(tmp_path):
    document = queued_document(tmp_path)
    result = extracted_result(usable=False, weak_second=True)

    outcome = process_pdf_document(
        str(document.uuid), extractor=extractor_returning(result)
    )

    document.refresh_from_db()
    assert outcome == "OCR_REQUIRED"
    assert document.processing_status == MedicalDocument.ProcessingStatus.OCR_REQUIRED
    assert document.document_text.text == result.text
    assert document.events.filter(
        event_type=MedicalDocumentEvent.EventType.PDF_OCR_REQUIRED
    ).exists()


def test_retry_reuses_canonical_result_without_duplicate_rows(tmp_path):
    document = queued_document(tmp_path)
    first_extractor = extractor_returning(extracted_result())
    process_pdf_document(str(document.uuid), extractor=first_extractor)
    retry_extractor = Mock()

    outcome = process_pdf_document(str(document.uuid), extractor=retry_extractor)

    assert outcome == "TEXT_EXTRACTED"
    retry_extractor.extract.assert_not_called()
    assert DocumentText.objects.count() == 1
    assert DocumentTextPage.objects.count() == 2


@override_settings(PDF_EXTRACTION_TIME_LIMIT=1800)
def test_fresh_claim_backs_off_but_stale_timed_out_claim_is_recovered(tmp_path):
    document = queued_document(tmp_path)
    document.processing_status = MedicalDocument.ProcessingStatus.PROCESSING
    document.processing_started_at = timezone.now()
    document.save(
        update_fields=("processing_status", "processing_started_at", "updated_at")
    )
    extractor = extractor_returning(extracted_result())

    assert process_pdf_document(str(document.uuid), extractor=extractor) == "PROCESSING"
    extractor.extract.assert_not_called()

    document.processing_started_at = timezone.now() - timedelta(seconds=1801)
    document.save(update_fields=("processing_started_at", "updated_at"))
    assert (
        process_pdf_document(str(document.uuid), extractor=extractor)
        == "TEXT_EXTRACTED"
    )
    extractor.extract.assert_called_once()


def test_explicit_reprocess_replaces_atomically_and_failure_preserves_canonical(
    tmp_path,
):
    document = queued_document(tmp_path)
    first = extracted_result()
    process_pdf_document(str(document.uuid), extractor=extractor_returning(first))

    failed = Mock()
    failed.extract.side_effect = PDFEncryptedError()
    assert (
        process_pdf_document(str(document.uuid), extractor=failed, reprocess=True)
        == "TEXT_EXTRACTED"
    )
    document.refresh_from_db()
    assert document.document_text.text == first.text

    replacement = extracted_result(weak_second=True)
    assert (
        process_pdf_document(
            str(document.uuid),
            extractor=extractor_returning(replacement),
            reprocess=True,
        )
        == "TEXT_EXTRACTED"
    )
    document.refresh_from_db()
    assert document.document_text.text == replacement.text
    assert DocumentText.objects.filter(document=document).count() == 1
    assert (
        DocumentTextPage.objects.filter(document_text__document=document).count() == 2
    )


def test_missing_blob_is_stable_nonretryable_failure(tmp_path):
    document = queued_document(tmp_path)
    document.stored_file.file.storage.delete(document.stored_file.file.name)

    outcome = process_pdf_document(str(document.uuid), extractor=Mock())

    document.refresh_from_db()
    assert outcome == "FAILED"
    assert document.processing_failure_code == "medical_file_missing"
    assert not DocumentText.objects.exists()


def test_integrity_mismatch_fails_before_extractor(tmp_path):
    document = queued_document(tmp_path)
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        with document.stored_file.file.open("wb") as stored:
            stored.write(b"tampered")
    extractor = Mock()

    outcome = process_pdf_document(str(document.uuid), extractor=extractor)

    document.refresh_from_db()
    document.stored_file.refresh_from_db()
    assert outcome == "FAILED"
    assert document.processing_failure_code == "medical_file_integrity_mismatch"
    assert document.stored_file.integrity_status == StoredFile.IntegrityStatus.CORRUPTED
    extractor.extract.assert_not_called()


def test_transient_storage_io_is_retryable_and_returns_to_queue(tmp_path):
    document = queued_document(tmp_path)
    with (
        patch.object(
            document.stored_file.file.storage,
            "open",
            side_effect=OSError("temporary storage outage"),
        ),
        pytest.raises(RetryablePDFProcessingError) as exc_info,
    ):
        process_pdf_document(str(document.uuid), extractor=Mock())

    document.refresh_from_db()
    assert exc_info.value.code == "medical_file_read_retryable"
    assert document.processing_status == MedicalDocument.ProcessingStatus.QUEUED
    assert document.processing_failure_code == "medical_file_read_retryable"


def test_encrypted_and_unexpected_parser_failures_are_controlled(tmp_path):
    encrypted = queued_document(tmp_path)
    encrypted_extractor = Mock()
    encrypted_extractor.extract.side_effect = PDFEncryptedError()

    assert (
        process_pdf_document(str(encrypted.uuid), extractor=encrypted_extractor)
        == "FAILED"
    )
    encrypted.refresh_from_db()
    assert encrypted.processing_failure_code == "pdf_encrypted"

    other = queued_document(
        tmp_path,
        content=b"second-synthetic-pdf",
    )
    broken = Mock()
    broken.extract.side_effect = RuntimeError("parser internal details")
    assert process_pdf_document(str(other.uuid), extractor=broken) == "FAILED"
    other.refresh_from_db()
    assert other.processing_failure_code == "pdf_extraction_failed"


def test_malformed_result_and_persistence_failure_are_controlled(tmp_path):
    malformed = queued_document(tmp_path)
    result = extracted_result()
    invalid = SimpleNamespace(**{**result.__dict__, "page_count": 3})

    assert (
        process_pdf_document(
            str(malformed.uuid), extractor=extractor_returning(invalid)
        )
        == "FAILED"
    )
    malformed.refresh_from_db()
    assert malformed.processing_failure_code == "pdf_malformed_result"

    persistence = queued_document(tmp_path, content=b"third-synthetic-pdf")
    with patch.object(
        DocumentText.objects,
        "create",
        side_effect=IntegrityError("database unavailable"),
    ):
        assert (
            process_pdf_document(
                str(persistence.uuid),
                extractor=extractor_returning(extracted_result()),
            )
            == "FAILED"
        )
    persistence.refresh_from_db()
    assert persistence.processing_failure_code == "pdf_persistence_failed"
    assert not DocumentText.objects.filter(document=persistence).exists()


@pytest.mark.parametrize(
    "override",
    [
        {"usable": "yes"},
        {"reason": "x" * 65},
        {"character_count": -1},
        {"metadata": {"extraction_method": "PDF_TEXT"}},
    ],
)
def test_unexpected_extractor_result_types_are_controlled(tmp_path, override):
    document = queued_document(tmp_path)
    result = extracted_result()
    invalid = SimpleNamespace(**{**result.__dict__, **override})

    outcome = process_pdf_document(
        str(document.uuid), extractor=extractor_returning(invalid)
    )

    document.refresh_from_db()
    assert outcome == "FAILED"
    assert document.processing_failure_code == "pdf_malformed_result"
    assert not DocumentText.objects.filter(document=document).exists()


def test_deleted_non_pdf_and_quarantined_documents_never_reach_extractor(tmp_path):
    deleted = queued_document(tmp_path)
    deleted.archive_status = MedicalDocument.ArchiveStatus.DELETED
    deleted.save(update_fields=("archive_status", "updated_at"))
    non_pdf = queued_document(tmp_path, content=b"image", mime_type="image/png")
    quarantined = queued_document(tmp_path, content=b"quarantined-pdf")
    quarantined.stored_file.integrity_status = StoredFile.IntegrityStatus.QUARANTINED
    quarantined.stored_file.save(update_fields=("integrity_status", "updated_at"))
    infected = queued_document(tmp_path, content=b"infected-pdf")
    infected.stored_file.malware_scan_status = StoredFile.MalwareScanStatus.INFECTED
    infected.stored_file.save(update_fields=("malware_scan_status", "updated_at"))
    extractor = Mock()

    assert process_pdf_document(str(deleted.uuid), extractor=extractor) == "SKIPPED"
    assert process_pdf_document(str(non_pdf.uuid), extractor=extractor) == "SKIPPED"
    assert process_pdf_document(str(quarantined.uuid), extractor=extractor) == "FAILED"
    assert process_pdf_document(str(infected.uuid), extractor=extractor) == "FAILED"
    extractor.extract.assert_not_called()


def test_final_persist_rechecks_active_document(tmp_path):
    document = queued_document(tmp_path)
    extractor = extractor_returning(extracted_result())

    def delete_during_extract(content):
        del content
        MedicalDocument.objects.filter(pk=document.pk).update(
            archive_status=MedicalDocument.ArchiveStatus.DELETED
        )
        return extracted_result()

    extractor.extract.side_effect = delete_during_extract

    assert process_pdf_document(str(document.uuid), extractor=extractor) == "SKIPPED"
    assert not DocumentText.objects.exists()


def test_processing_logs_are_allowlisted_and_never_contain_medical_text(
    tmp_path,
    caplog,
):
    document = queued_document(tmp_path)
    result = extracted_result()
    private_text = result.pages[0].text

    with caplog.at_level(logging.INFO, logger="processing.services"):
        process_pdf_document(str(document.uuid), extractor=extractor_returning(result))

    rendered = " ".join(record.getMessage() for record in caplog.records)
    assert "PDF extraction completed" in rendered
    assert private_text not in rendered
    assert "Synthetic Processing Owner" not in rendered
    assert document.stored_file.original_filename not in rendered
    completion = next(
        record
        for record in caplog.records
        if record.getMessage() == "PDF extraction completed"
    )
    assert completion.document_uuid == str(document.uuid)
    assert completion.processing_status == "TEXT_EXTRACTED"
    assert completion.page_count == 2
    assert completion.duration_ms >= 0
    assert all(record.exc_info is None for record in caplog.records)
    assert private_text not in str(
        list(document.events.values_list("metadata", flat=True))
    )


@override_settings(
    PDF_TEXT_MIN_CHARS=20,
    PDF_TEXT_MIN_PAGE_CHARS=10,
    PDF_TEXT_MIN_TEXT_PAGE_RATIO=0.5,
)
def test_real_pymupdf_pipeline_updates_state_without_mutating_original(tmp_path):
    content = pdf_bytes(
        "First digital medical page with useful content 12345",
        "Second digital medical page with useful content 67890",
    )
    document = queued_document(tmp_path, content=content)
    original_hash = document.stored_file.sha256

    assert process_pdf_document(str(document.uuid)) == "TEXT_EXTRACTED"

    document.refresh_from_db()
    with document.stored_file.file.open("rb") as original:
        assert original.read() == content
    assert document.stored_file.sha256 == original_hash
    assert document.document_text.page_count == 2


@override_settings(
    PDF_TEXT_MIN_CHARS=20,
    PDF_TEXT_MIN_PAGE_CHARS=10,
    PDF_TEXT_MIN_TEXT_PAGE_RATIO=0.5,
)
def test_real_image_only_pdf_routes_to_ocr_required_without_ocr(tmp_path):
    document = queued_document(tmp_path, content=pdf_bytes("", ""))

    assert process_pdf_document(str(document.uuid)) == "OCR_REQUIRED"

    document.refresh_from_db()
    assert document.processing_status == MedicalDocument.ProcessingStatus.OCR_REQUIRED
    assert document.document_text.usable is False
    assert list(
        document.document_text.pages.values_list("requires_ocr", flat=True)
    ) == [True, True]
