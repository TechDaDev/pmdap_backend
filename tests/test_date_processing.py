import json
import logging
from datetime import date

import pytest
from django.db import IntegrityError, transaction
from django.test import override_settings

from documents.models import MedicalDocument, MedicalDocumentEvent
from processing.date_services import process_date_candidates
from processing.models import DateCandidate, DocumentTextPage
from tests.test_document_text_models import document_text, medical_document

pytestmark = pytest.mark.django_db


def prepared_document(text, *, source=DocumentTextPage.EffectiveSource.PDF_TEXT):
    document = medical_document()
    extracted = document_text(document)
    extracted.text = text
    extracted.character_count = len(text)
    extracted.meaningful_character_count = len(text.replace(" ", ""))
    extracted.has_pages_requiring_ocr = False
    extracted.save(
        update_fields=(
            "text",
            "character_count",
            "meaningful_character_count",
            "has_pages_requiring_ocr",
            "updated_at",
        )
    )
    DocumentTextPage.objects.create(
        document_text=extracted,
        page_number=1,
        text=text,
        native_text=text if source == "PDF_TEXT" else "",
        ocr_text=text if source == "OCR" else "",
        meaningful_character_count=len(text.replace(" ", "")),
        effective_source=source,
        ocr_completed=source == "OCR",
    )
    return document


@pytest.mark.parametrize(
    "date_source",
    [
        MedicalDocument.DateSource.USER_ENTERED,
        MedicalDocument.DateSource.USER_CORRECTED,
    ],
)
def test_processing_persists_ranked_candidates_and_preserves_manual_date(
    date_source,
):
    document = prepared_document(
        "DOB: 21/06/1985\nCollection Date: 12/07/2026\n"
        "Report Date: 13/07/2026\nPrinted: 14/07/2026"
    )
    document.document_date = date(2026, 3, 10)
    document.date_source = date_source
    document.date_verified = True
    document.save(
        update_fields=("document_date", "date_source", "date_verified", "updated_at")
    )

    outcome = process_date_candidates(str(document.uuid))

    document.refresh_from_db()
    candidates = list(document.date_candidates.all())
    assert outcome == MedicalDocument.ProcessingStatus.DATE_DETECTED
    assert len(candidates) == 4
    assert sum(candidate.is_suggested for candidate in candidates) == 1
    suggested = next(candidate for candidate in candidates if candidate.is_suggested)
    assert suggested.detected_date == date(2026, 7, 13)
    assert document.document_date == date(2026, 3, 10)
    assert document.date_source == date_source
    assert document.date_verified is True


def test_arabic_ocr_page_preserves_raw_value_and_source():
    document = prepared_document(
        "تاريخ التقرير: ١٤/٠٣/٢٠٢٦",
        source=DocumentTextPage.EffectiveSource.OCR,
    )

    process_date_candidates(str(document.uuid))

    candidate = document.date_candidates.get()
    assert candidate.detected_date == date(2026, 3, 14)
    assert candidate.raw_value == "١٤/٠٣/٢٠٢٦"
    assert candidate.normalized_value == "14/03/2026"
    assert candidate.source == DateCandidate.Source.OCR
    assert candidate.pipeline_version == "m9-date-v1"


def test_no_date_is_stable_non_failure_without_candidates():
    document = prepared_document("Synthetic report contains no date expression.")

    outcome = process_date_candidates(str(document.uuid))

    document.refresh_from_db()
    assert outcome == MedicalDocument.ProcessingStatus.DATE_NOT_FOUND
    assert document.processing_failure_code == ""
    assert not document.date_candidates.exists()
    assert document.events.filter(
        event_type=MedicalDocumentEvent.EventType.DATE_NOT_FOUND
    ).exists()


def test_repeated_delivery_reuses_canonical_candidate_set():
    document = prepared_document("Report Date: 14/03/2026")

    first = process_date_candidates(str(document.uuid))
    candidate_uuid = document.date_candidates.get().uuid
    second = process_date_candidates(str(document.uuid))

    assert first == second == MedicalDocument.ProcessingStatus.DATE_DETECTED
    assert document.date_candidates.count() == 1
    assert document.date_candidates.get().uuid == candidate_uuid


def test_deleted_during_detection_is_not_resurrected():
    document = prepared_document("Report Date: 14/03/2026")

    def detector(*args, **kwargs):
        MedicalDocument.objects.filter(pk=document.pk).update(
            archive_status=MedicalDocument.ArchiveStatus.DELETED
        )
        from processing.dates import detect_page_dates

        return detect_page_dates(*args, **kwargs)

    outcome = process_date_candidates(str(document.uuid), detector=detector)

    document.refresh_from_db()
    assert outcome == "SKIPPED"
    assert document.archive_status == MedicalDocument.ArchiveStatus.DELETED
    assert not document.date_candidates.exists()


def test_late_failure_cannot_overwrite_success_state():
    document = prepared_document("Report Date: 14/03/2026")

    def stale_detector(*args, **kwargs):
        del args, kwargs
        MedicalDocument.objects.filter(pk=document.pk).update(
            processing_status=MedicalDocument.ProcessingStatus.DATE_DETECTED,
            processing_started_at=None,
        )
        raise RuntimeError("stale worker failed")

    outcome = process_date_candidates(str(document.uuid), detector=stale_detector)

    document.refresh_from_db()
    assert outcome == MedicalDocument.ProcessingStatus.DATE_DETECTED
    assert document.processing_failure_code == ""


def test_parser_failure_is_controlled_and_preserves_canonical_text():
    document = prepared_document("Report Date: 14/03/2026")
    original_text = document.document_text.text

    def broken_detector(*args, **kwargs):
        del args, kwargs
        raise ValueError("private parser detail")

    outcome = process_date_candidates(str(document.uuid), detector=broken_detector)

    document.refresh_from_db()
    assert outcome == MedicalDocument.ProcessingStatus.FAILED
    assert document.processing_failure_code == "date_processing_failed"
    assert document.document_text.text == original_text


@override_settings(DATE_MAX_CANDIDATES_PER_DOCUMENT=1)
def test_candidate_limit_fails_closed():
    document = prepared_document("Report Date: 14/03/2026\nIssue Date: 15/03/2026")

    outcome = process_date_candidates(str(document.uuid))

    document.refresh_from_db()
    assert outcome == MedicalDocument.ProcessingStatus.FAILED
    assert document.processing_failure_code == "date_candidate_limit_exceeded"
    assert not document.date_candidates.exists()


def test_events_and_logs_never_include_dates_or_context(caplog):
    document = prepared_document("Report Date: 14/03/2026 secret diagnosis")

    with caplog.at_level(logging.INFO):
        process_date_candidates(str(document.uuid))

    event_payload = json.dumps(
        list(document.events.values("event_type", "metadata")), ensure_ascii=False
    )
    assert "14/03/2026" not in event_payload
    assert "secret diagnosis" not in event_payload
    assert "14/03/2026" not in caplog.text
    assert "secret diagnosis" not in caplog.text


def test_database_constraints_bound_score_occurrence_and_suggestion():
    document = prepared_document("Report Date: 14/03/2026")
    process_date_candidates(str(document.uuid))
    candidate = document.date_candidates.get()

    with pytest.raises(IntegrityError), transaction.atomic():
        DateCandidate.objects.create(
            document=document,
            detected_date=date(2026, 3, 15),
            raw_value="15/03/2026",
            normalized_value="15/03/2026",
            candidate_type=DateCandidate.CandidateType.REPORT_DATE,
            score=0.9,
            page_number=1,
            context="Report Date: 15/03/2026",
            source=DateCandidate.Source.PDF_TEXT,
            occurrence_index=50,
            parsing_rule="DMY_NUMERIC",
            pipeline_version="m9-date-v1",
            is_suggested=True,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        candidate.score = 1.1
        candidate.save(update_fields=("score", "updated_at"))
