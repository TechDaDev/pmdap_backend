from unittest.mock import patch

import pytest

from documents.models import MedicalDocument, MedicalDocumentEvent
from processing.date_services import process_date_candidates, schedule_date_processing
from processing.extraction import PDFTextPageResult, PDFTextResult
from processing.models import DateCandidate
from processing.ocr_services import process_ocr_document
from processing.services import process_pdf_document
from tests.test_ocr_processing import FakeEngine, make_document
from tests.test_pdf_processing import (
    extracted_result,
    extractor_returning,
    queued_document,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def private_storage(settings, tmp_path):
    settings.MEDICAL_FILE_ROOT = tmp_path


def test_date_dispatch_occurs_only_after_commit(
    django_capture_on_commit_callbacks, tmp_path
):
    document = queued_document(tmp_path)
    with (
        patch("processing.tasks.detect_document_dates.delay") as delay,
        django_capture_on_commit_callbacks(execute=True),
    ):
        schedule_date_processing(document)
        delay.assert_not_called()

    delay.assert_called_once_with(str(document.uuid))
    assert document.events.filter(
        event_type=MedicalDocumentEvent.EventType.DATE_PROCESSING_QUEUED
    ).exists()


def test_native_pdf_success_schedules_page_date_processing(tmp_path):
    document = queued_document(tmp_path)

    with patch(
        "processing.date_services.schedule_page_date_processing"
    ) as schedule_page_dates:
        outcome = process_pdf_document(
            str(document.uuid),
            extractor=extractor_returning(extracted_result()),
        )

    assert outcome == MedicalDocument.ProcessingStatus.TEXT_EXTRACTED
    # Two-page PDF -> one independent page date task per page.
    assert schedule_page_dates.call_count == 2


def test_pdf_requiring_ocr_does_not_schedule_dates_prematurely(tmp_path):
    document = queued_document(tmp_path)

    with (
        patch("processing.ocr_services.schedule_ocr") as schedule_ocr,
        patch("processing.date_services.schedule_date_processing") as schedule_dates,
    ):
        process_pdf_document(
            str(document.uuid),
            extractor=extractor_returning(extracted_result(weak_second=True)),
        )

    schedule_ocr.assert_called_once()
    schedule_dates.assert_not_called()


def test_ocr_success_schedules_date_processing(tmp_path):
    document = make_document(tmp_path)

    with patch("processing.date_services.schedule_date_processing") as schedule_dates:
        outcome = process_ocr_document(str(document.uuid), engine=FakeEngine())

    assert outcome == MedicalDocument.ProcessingStatus.TEXT_EXTRACTED
    schedule_dates.assert_called_once()


def test_native_pdf_to_canonical_text_to_date_candidate(tmp_path):
    document = queued_document(tmp_path)
    first_page = "Synthetic report\nReport Date: 14/03/2026\nNo patient data"
    second_page = "Synthetic clinical observations without another date"
    text = f"{first_page}\n\f\n{second_page}"
    extracted = PDFTextResult(
        text=text,
        page_count=2,
        pages=(
            PDFTextPageResult(1, first_page, len(first_page), False),
            PDFTextPageResult(2, second_page, len(second_page), False),
        ),
        character_count=len(text),
        usable=True,
        reason="usable_pdf_text",
        metadata={
            "extraction_method": "PDF_TEXT",
            "extractor_name": "PyMuPDF",
            "extractor_version": "1.28.0",
            "pipeline_version": "m7-v1",
            "meaningful_character_count": len(first_page) + len(second_page),
            "pages_requiring_ocr": [],
        },
    )

    with patch("processing.date_services.schedule_date_processing"):
        assert (
            process_pdf_document(
                str(document.uuid), extractor=extractor_returning(extracted)
            )
            == MedicalDocument.ProcessingStatus.TEXT_EXTRACTED
        )
    assert process_date_candidates(str(document.uuid)) == "AWAITING_CONFIRMATION"

    detected = DateCandidate.objects.get(document=document, is_suggested=True)
    assert str(detected.detected_date) == "2026-03-14"
    assert detected.candidate_type == DateCandidate.CandidateType.REPORT_DATE
    assert detected.source == DateCandidate.Source.PDF_TEXT


def test_image_to_ocr_canonical_text_to_date_candidate(tmp_path):
    document = make_document(tmp_path)

    with patch("processing.date_services.schedule_date_processing"):
        assert (
            process_ocr_document(str(document.uuid), engine=FakeEngine())
            == MedicalDocument.ProcessingStatus.TEXT_EXTRACTED
        )
    assert process_date_candidates(str(document.uuid)) == "AWAITING_CONFIRMATION"

    detected = DateCandidate.objects.get(document=document, is_suggested=True)
    assert str(detected.detected_date) == "2026-03-14"
    assert detected.candidate_type == DateCandidate.CandidateType.REPORT_DATE
    assert detected.source == DateCandidate.Source.OCR
