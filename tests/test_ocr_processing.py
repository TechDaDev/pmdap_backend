import hashlib
import io
import logging
from dataclasses import replace
from datetime import date
from unittest.mock import Mock, patch

import pytest
from django.core.files.base import ContentFile
from django.db import DatabaseError
from django.test import override_settings
from PIL import Image

from accounts.models import User
from documents.models import MedicalDocument, MedicalDocumentEvent, StoredFile
from patients.models import PatientProfile
from processing.extraction import PDFTextPageResult, PDFTextResult
from processing.models import DocumentText, DocumentTextPage
from processing.ocr import (
    OCREngineUnavailableError,
    OCRImageDecodeError,
    OCRLine,
    OCRResult,
    PDFPageRenderError,
)
from processing.ocr_services import (
    RetryableOCRProcessingError,
    process_ocr_document,
)
from processing.services import process_pdf_document

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def private_storage(settings, tmp_path):
    settings.MEDICAL_FILE_ROOT = tmp_path


def image_content(image_format="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (320, 120), "white").save(output, format=image_format)
    return output.getvalue()


def make_document(tmp_path, *, mime_type="image/png", content=None, page_count=None):
    sequence = User.objects.count() + 1
    user = User.objects.create_user(
        email=f"ocr-owner-{sequence}@example.com",
        password="A-complex-password-2026!",
        status=User.Status.ACTIVE,
    )
    patient = PatientProfile.objects.create(
        user=user,
        digital_id=f"{sequence + 700:017d}",
        full_name="Synthetic OCR Owner",
        date_of_birth=date(1990, 1, 2),
        sex=PatientProfile.Sex.UNSPECIFIED,
        nationality="IQ",
    )
    if content is None:
        content = image_content("JPEG" if mime_type == "image/jpeg" else "PNG")
    digest = hashlib.sha256(content).hexdigest()
    stored = StoredFile(
        original_filename="synthetic-report",
        mime_type=mime_type,
        size_bytes=len(content),
        sha256=digest,
        page_count=page_count,
        integrity_status=StoredFile.IntegrityStatus.VALID,
    )
    stored.file.save(f"medical/ocr-{sequence}", ContentFile(content), save=False)
    stored.save()
    document = MedicalDocument.objects.create(
        patient=patient,
        uploaded_by=user,
        stored_file=stored,
        content_sha256=digest,
        document_type=MedicalDocument.DocumentType.MEDICAL_REPORT,
    )
    return document


def result(text="Patient Report\nتاريخ التقرير: ١٤/٠٣/٢٠٢٦", confidence=0.9):
    lines = tuple(OCRLine(line, float(confidence)) for line in text.splitlines())
    return OCRResult(
        text=text,
        lines=lines,
        mean_confidence=float(confidence),
        minimum_confidence=float(confidence),
        engine_name="fake-paddleocr",
        engine_version="3.7.0",
        duration_ms=12,
    )


class FakeEngine:
    def __init__(self, outputs=None):
        self.outputs = iter(outputs or [result()])
        self.images = []

    def extract_image(self, image):
        self.images.append(image.size)
        return next(self.outputs)


class FakeRenderer:
    def __init__(self):
        self.pages = []

    def render(self, content, page_number):
        self.pages.append(page_number)
        return Image.new("RGB", (100, 100), "white")


@pytest.mark.parametrize("mime_type", ["image/jpeg", "image/png"])
def test_image_ocr_uses_shared_text_domain_and_preserves_original(tmp_path, mime_type):
    document = make_document(tmp_path, mime_type=mime_type)
    original_hash = document.stored_file.sha256

    outcome = process_ocr_document(str(document.uuid), engine=FakeEngine())

    document.refresh_from_db()
    page = document.document_text.pages.get()
    assert outcome == MedicalDocument.ProcessingStatus.TEXT_EXTRACTED
    assert document.processing_status == MedicalDocument.ProcessingStatus.TEXT_EXTRACTED
    assert document.stored_file.sha256 == original_hash
    assert document.document_text.extraction_method == DocumentText.ExtractionMethod.OCR
    assert document.document_text.text == "Patient Report\nتاريخ التقرير: ١٤/٠٣/٢٠٢٦"
    assert page.native_text == ""
    assert page.ocr_text == page.text
    assert page.effective_source == DocumentTextPage.EffectiveSource.OCR
    assert page.ocr_completed is True
    assert page.requires_ocr is False
    assert document.events.filter(
        event_type=MedicalDocumentEvent.EventType.OCR_COMPLETED
    ).exists()


def pdf_result(*, all_weak=False):
    pages = (
        PDFTextPageResult(
            1,
            "scan-one" if all_weak else "Native page one",
            7 if all_weak else 13,
            all_weak,
        ),
        PDFTextPageResult(2, "scan-two", 7, True),
    )
    aggregate = "\n\f\n".join(page.text for page in pages)
    return PDFTextResult(
        text=aggregate,
        page_count=2,
        pages=pages,
        character_count=len(aggregate),
        usable=not all_weak,
        reason="usable_pdf_text" if not all_weak else "insufficient_meaningful_text",
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


def prepare_pdf_document(tmp_path, *, all_weak=False):
    content = b"synthetic-pdf-for-mocked-renderer"
    document = make_document(
        tmp_path,
        mime_type="application/pdf",
        content=content,
        page_count=2,
    )
    document.processing_status = MedicalDocument.ProcessingStatus.QUEUED
    document.save(update_fields=("processing_status", "updated_at"))
    extractor = Mock()
    extractor.extract.return_value = pdf_result(all_weak=all_weak)
    with patch("processing.tasks.ocr_medical_document.delay"):
        process_pdf_document(str(document.uuid), extractor=extractor)
    document.refresh_from_db()
    return document


def test_mixed_pdf_ocrs_only_weak_page_and_preserves_native_page(tmp_path):
    document = prepare_pdf_document(tmp_path)
    renderer = FakeRenderer()
    engine = FakeEngine([result("OCR page two")])

    outcome = process_ocr_document(str(document.uuid), engine=engine, renderer=renderer)

    document.refresh_from_db()
    pages = list(document.document_text.pages.all())
    assert outcome == MedicalDocument.ProcessingStatus.TEXT_EXTRACTED
    assert renderer.pages == [2]
    assert len(engine.images) == 1
    assert pages[0].text == "Native page one"
    assert pages[0].native_text == "Native page one"
    assert pages[0].ocr_text == ""
    assert pages[0].effective_source == DocumentTextPage.EffectiveSource.PDF_TEXT
    assert pages[1].native_text == "scan-two"
    assert pages[1].ocr_text == "OCR page two"
    assert pages[1].text == "OCR page two"
    assert document.document_text.text == "Native page one\n\f\nOCR page two"
    assert (
        document.document_text.extraction_method == DocumentText.ExtractionMethod.HYBRID
    )


def test_image_only_pdf_transitions_from_ocr_required_to_text_extracted(tmp_path):
    document = prepare_pdf_document(tmp_path, all_weak=True)
    assert document.processing_status == MedicalDocument.ProcessingStatus.OCR_REQUIRED
    renderer = FakeRenderer()

    outcome = process_ocr_document(
        str(document.uuid),
        engine=FakeEngine([result("OCR page one"), result("OCR page two")]),
        renderer=renderer,
    )

    document.refresh_from_db()
    assert outcome == MedicalDocument.ProcessingStatus.TEXT_EXTRACTED
    assert renderer.pages == [1, 2]
    assert document.document_text.extraction_method == DocumentText.ExtractionMethod.OCR
    assert not document.document_text.has_pages_requiring_ocr
    assert document.document_text.text == "OCR page one\n\f\nOCR page two"


def test_repeated_delivery_reuses_canonical_image_result(tmp_path):
    document = make_document(tmp_path)
    first = FakeEngine()
    assert process_ocr_document(str(document.uuid), engine=first) == "TEXT_EXTRACTED"
    second = FakeEngine()

    assert process_ocr_document(str(document.uuid), engine=second) == "TEXT_EXTRACTED"
    assert not second.images
    assert DocumentText.objects.count() == 1
    assert DocumentTextPage.objects.count() == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("archive_status", MedicalDocument.ArchiveStatus.DELETED),
        ("integrity_status", StoredFile.IntegrityStatus.CORRUPTED),
        ("integrity_status", StoredFile.IntegrityStatus.QUARANTINED),
        ("malware_scan_status", StoredFile.MalwareScanStatus.INFECTED),
        ("malware_scan_status", StoredFile.MalwareScanStatus.ERROR),
    ],
)
def test_deleted_corrupt_quarantined_and_disallowed_documents_never_ocr(
    tmp_path, field, value
):
    document = make_document(tmp_path)
    target = document if field == "archive_status" else document.stored_file
    setattr(target, field, value)
    target.save(update_fields=(field, "updated_at"))
    engine = FakeEngine()

    outcome = process_ocr_document(str(document.uuid), engine=engine)

    assert outcome in {"SKIPPED", MedicalDocument.ProcessingStatus.FAILED}
    assert not engine.images
    assert not DocumentText.objects.exists()


def test_deletion_during_ocr_prevents_final_persistence(tmp_path):
    document = make_document(tmp_path)

    class DeletingEngine(FakeEngine):
        def extract_image(self, image):
            MedicalDocument.objects.filter(pk=document.pk).update(
                archive_status=MedicalDocument.ArchiveStatus.DELETED
            )
            return super().extract_image(image)

    assert (
        process_ocr_document(str(document.uuid), engine=DeletingEngine()) == "SKIPPED"
    )
    assert not DocumentText.objects.exists()


def test_engine_unavailable_is_controlled_and_preserves_original(tmp_path):
    document = make_document(tmp_path)
    original_hash = document.stored_file.sha256
    engine = Mock()
    engine.extract_image.side_effect = OCREngineUnavailableError()

    assert process_ocr_document(str(document.uuid), engine=engine) == "FAILED"
    document.refresh_from_db()
    assert document.processing_failure_code == "ocr_engine_unavailable"
    assert document.stored_file.sha256 == original_hash


def test_transient_storage_failure_is_retryable_and_bounded_by_task(tmp_path):
    document = make_document(tmp_path)
    with (
        patch(
            "processing.ocr_services._read_verified_content",
            side_effect=OSError("temporary resource failure"),
        ),
        pytest.raises(RetryableOCRProcessingError),
    ):
        process_ocr_document(str(document.uuid), engine=FakeEngine())
    document.refresh_from_db()
    assert document.processing_status == MedicalDocument.ProcessingStatus.UPLOADED
    assert document.processing_failure_code == "ocr_resource_retryable"


def test_transient_engine_resource_failure_is_retryable(tmp_path):
    document = make_document(tmp_path)
    engine = Mock()
    engine.extract_image.side_effect = MemoryError("synthetic memory pressure")
    with pytest.raises(RetryableOCRProcessingError):
        process_ocr_document(str(document.uuid), engine=engine)
    document.refresh_from_db()
    assert document.processing_status == MedicalDocument.ProcessingStatus.UPLOADED
    assert document.processing_failure_code == "ocr_resource_retryable"


def test_malformed_result_and_document_text_overflow_fail_closed(tmp_path):
    malformed_document = make_document(tmp_path)
    malformed_engine = Mock()
    malformed_engine.extract_image.return_value = object()
    assert (
        process_ocr_document(str(malformed_document.uuid), engine=malformed_engine)
        == "FAILED"
    )
    malformed_document.refresh_from_db()
    assert malformed_document.processing_failure_code == "ocr_malformed_result"

    overflow_document = make_document(
        tmp_path, content=image_content("JPEG"), mime_type="image/jpeg"
    )
    with override_settings(OCR_MAX_TEXT_CHARS_PER_DOCUMENT=3):
        assert (
            process_ocr_document(
                str(overflow_document.uuid), engine=FakeEngine([result("long")])
            )
            == "FAILED"
        )
    overflow_document.refresh_from_db()
    assert overflow_document.processing_failure_code == "ocr_text_limit_exceeded"


@pytest.mark.parametrize(
    "malformed_result",
    [
        replace(result(), mean_confidence=float("nan")),
        replace(result(), minimum_confidence=2.0),
        replace(result(), mean_confidence=None),
    ],
)
def test_replaceable_engine_confidence_contract_fails_closed(
    tmp_path, malformed_result
):
    document = make_document(tmp_path)
    assert (
        process_ocr_document(str(document.uuid), engine=FakeEngine([malformed_result]))
        == "FAILED"
    )
    document.refresh_from_db()
    assert document.processing_failure_code == "ocr_malformed_result"


def test_replaceable_engine_per_page_text_overflow_fails_closed(tmp_path):
    document = make_document(tmp_path)
    with override_settings(
        OCR_MAX_TEXT_CHARS_PER_PAGE=3,
        OCR_MAX_TEXT_CHARS_PER_DOCUMENT=100,
    ):
        assert (
            process_ocr_document(
                str(document.uuid), engine=FakeEngine([result("long")])
            )
            == "FAILED"
        )
    document.refresh_from_db()
    assert document.processing_failure_code == "ocr_text_limit_exceeded"


@pytest.mark.parametrize(
    ("read_result", "expected_code"),
    [
        ((None, "medical_file_missing"), "medical_file_missing"),
        ((None, "medical_file_integrity_mismatch"), "medical_file_integrity_mismatch"),
    ],
)
def test_missing_blob_and_integrity_mismatch_fail_without_replacing_original(
    tmp_path, read_result, expected_code
):
    document = make_document(tmp_path)
    original_hash = document.stored_file.sha256
    with patch(
        "processing.ocr_services._read_verified_content", return_value=read_result
    ):
        assert process_ocr_document(str(document.uuid), engine=FakeEngine()) == "FAILED"

    document.refresh_from_db()
    assert document.processing_failure_code == expected_code
    assert document.stored_file.sha256 == original_hash
    assert not DocumentText.objects.filter(document=document).exists()


def test_engine_exception_image_decode_and_pdf_render_fail_closed(tmp_path):
    engine_failure = make_document(tmp_path)
    broken_engine = Mock()
    broken_engine.extract_image.side_effect = RuntimeError("synthetic engine failure")
    assert (
        process_ocr_document(str(engine_failure.uuid), engine=broken_engine) == "FAILED"
    )
    engine_failure.refresh_from_db()
    assert engine_failure.processing_failure_code == "ocr_failed"

    decode_failure = make_document(tmp_path)
    preprocessor = Mock()
    preprocessor.prepare.side_effect = OCRImageDecodeError()
    assert (
        process_ocr_document(
            str(decode_failure.uuid),
            engine=FakeEngine(),
            preprocessor=preprocessor,
        )
        == "FAILED"
    )
    decode_failure.refresh_from_db()
    assert decode_failure.processing_failure_code == "ocr_image_decode_failed"

    render_failure = prepare_pdf_document(tmp_path)
    renderer = Mock()
    renderer.render.side_effect = PDFPageRenderError()
    assert (
        process_ocr_document(
            str(render_failure.uuid),
            engine=FakeEngine(),
            renderer=renderer,
        )
        == "TEXT_EXTRACTED"
    )
    render_failure.refresh_from_db()
    assert render_failure.processing_failure_code == "ocr_pdf_render_failed"
    assert (
        render_failure.document_text.pages.get(page_number=1).text == "Native page one"
    )


def test_database_write_failure_preserves_original_document_and_blob(tmp_path):
    document = make_document(tmp_path)
    original_hash = document.stored_file.sha256
    with patch.object(
        DocumentText.objects,
        "create",
        side_effect=DatabaseError("synthetic persistence failure"),
    ):
        assert process_ocr_document(str(document.uuid), engine=FakeEngine()) == "FAILED"

    document.refresh_from_db()
    assert document.processing_failure_code == "ocr_persistence_failed"
    assert document.stored_file.sha256 == original_hash
    assert not DocumentText.objects.filter(document=document).exists()


def test_ocr_logs_and_events_never_contain_medical_text(tmp_path, caplog):
    document = make_document(tmp_path)
    secret = "Secret diagnosis value 9876"
    with caplog.at_level(logging.INFO, logger="processing.ocr_services"):
        process_ocr_document(str(document.uuid), engine=FakeEngine([result(secret)]))

    assert secret not in caplog.text
    assert secret not in str(list(document.events.values_list("metadata", flat=True)))
