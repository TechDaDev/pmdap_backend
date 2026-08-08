from datetime import date

import pytest
from django.db import IntegrityError, transaction

from accounts.models import User
from documents.models import MedicalDocument, MedicalDocumentEvent, StoredFile
from patients.models import PatientProfile
from processing.models import DocumentText, DocumentTextPage

pytestmark = pytest.mark.django_db


def medical_document():
    user = User.objects.create_user(
        email="text-model-owner@example.com",
        password="A-complex-password-2026!",
        status=User.Status.ACTIVE,
    )
    patient = PatientProfile.objects.create(
        user=user,
        digital_id="12345678901234567",
        full_name="Synthetic Text Owner",
        date_of_birth=date(1990, 1, 2),
        sex=PatientProfile.Sex.UNSPECIFIED,
        nationality="IQ",
    )
    stored = StoredFile.objects.create(
        file="medical/text-model.pdf",
        original_filename="report.pdf",
        mime_type="application/pdf",
        size_bytes=123,
        sha256="a" * 64,
        page_count=2,
        integrity_status=StoredFile.IntegrityStatus.VALID,
    )
    return MedicalDocument.objects.create(
        patient=patient,
        uploaded_by=user,
        stored_file=stored,
        content_sha256=stored.sha256,
        document_type=MedicalDocument.DocumentType.MEDICAL_REPORT,
        processing_status=MedicalDocument.ProcessingStatus.TEXT_EXTRACTED,
    )


def document_text(document):
    return DocumentText.objects.create(
        document=document,
        text="Page one\n\f\nPage two",
        page_count=2,
        character_count=22,
        meaningful_character_count=14,
        usable=True,
        usability_reason="usable_pdf_text",
        has_pages_requiring_ocr=True,
        extraction_method=DocumentText.ExtractionMethod.PDF_TEXT,
        extractor_name="PyMuPDF",
        extractor_version="1.28.0",
        pipeline_version="m7-v1",
    )


def test_document_text_persists_one_canonical_result_with_provenance():
    document = medical_document()
    extracted = document_text(document)

    assert extracted.document == document
    assert document.document_text == extracted
    assert extracted.extraction_method == "PDF_TEXT"
    assert extracted.usable is True
    assert extracted.has_pages_requiring_ocr is True

    with pytest.raises(IntegrityError), transaction.atomic():
        document_text(document)


def test_document_text_pages_are_ordered_and_unique_per_page_number():
    extracted = document_text(medical_document())
    second = DocumentTextPage.objects.create(
        document_text=extracted,
        page_number=2,
        text="Page two",
        meaningful_character_count=7,
        requires_ocr=True,
    )
    first = DocumentTextPage.objects.create(
        document_text=extracted,
        page_number=1,
        text="Page one",
        meaningful_character_count=7,
        requires_ocr=False,
    )

    assert list(extracted.pages.all()) == [first, second]
    with pytest.raises(IntegrityError), transaction.atomic():
        DocumentTextPage.objects.create(
            document_text=extracted,
            page_number=1,
            text="duplicate",
            meaningful_character_count=9,
        )


def test_m7_processing_states_and_events_are_controlled_values():
    assert MedicalDocument.ProcessingStatus.OCR_REQUIRED == "OCR_REQUIRED"
    assert set(MedicalDocumentEvent.EventType.values) >= {
        "PDF_EXTRACTION_QUEUED",
        "PDF_EXTRACTION_STARTED",
        "PDF_TEXT_EXTRACTED",
        "PDF_OCR_REQUIRED",
        "PDF_EXTRACTION_FAILED",
    }
