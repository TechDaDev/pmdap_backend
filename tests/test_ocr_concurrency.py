import threading
from unittest.mock import Mock, patch

import pytest
from django.db import close_old_connections, connection
from django.test import override_settings

from documents.models import MedicalDocument
from processing.extraction import PDFTextPageResult, PDFTextResult
from processing.models import DocumentText, DocumentTextPage
from processing.ocr_services import _mark_failure, process_ocr_document
from processing.services import process_pdf_document
from tests.test_ocr_processing import (
    FakeRenderer,
    make_document,
    pdf_result,
    prepare_pdf_document,
    result,
)

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgresql]


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL-only OCR concurrency test")


class BlockingEngine:
    def __init__(self, output=None):
        self.output = output or result()
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def extract_image(self, image):
        self.calls += 1
        self.started.set()
        assert self.release.wait(timeout=10)
        return self.output


def run_worker(document_uuid, engine, *, renderer=None):
    results = []
    failures = []

    def operation():
        close_old_connections()
        try:
            results.append(
                process_ocr_document(
                    document_uuid,
                    engine=engine,
                    renderer=renderer,
                )
            )
        except Exception as exc:
            failures.append(exc)
        finally:
            close_old_connections()

    thread = threading.Thread(target=operation)
    thread.start()
    return thread, results, failures


def finish(thread, engine):
    engine.release.set()
    thread.join(timeout=20)
    assert not thread.is_alive()


def test_two_image_workers_create_one_canonical_result(tmp_path):
    require_postgresql()
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        document = make_document(tmp_path)
        engine = BlockingEngine()
        thread, results, failures = run_worker(str(document.uuid), engine)
        assert engine.started.wait(timeout=10)
        second_engine = Mock()
        second = process_ocr_document(str(document.uuid), engine=second_engine)
        finish(thread, engine)

    document.refresh_from_db()
    assert not failures
    assert results == ["TEXT_EXTRACTED"]
    assert second == "OCR_PROCESSING"
    second_engine.extract_image.assert_not_called()
    assert DocumentText.objects.filter(document=document).count() == 1
    assert (
        DocumentTextPage.objects.filter(document_text__document=document).count() == 1
    )


def test_two_pdf_page_workers_create_one_ocr_page_result(tmp_path):
    require_postgresql()
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        document = prepare_pdf_document(tmp_path)
        engine = BlockingEngine(result("OCR page two"))
        thread, results, failures = run_worker(
            str(document.uuid), engine, renderer=FakeRenderer()
        )
        assert engine.started.wait(timeout=10)
        second = process_ocr_document(
            str(document.uuid), engine=Mock(), renderer=FakeRenderer()
        )
        finish(thread, engine)

    assert not failures
    assert results == ["TEXT_EXTRACTED"]
    assert second == "OCR_PROCESSING"
    page = DocumentTextPage.objects.get(document_text__document=document, page_number=2)
    assert page.ocr_text == "OCR page two"
    assert page.ocr_completed is True


def test_late_failure_cannot_overwrite_success(tmp_path):
    require_postgresql()
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        document = make_document(tmp_path)
        assert (
            process_ocr_document(str(document.uuid), engine=MockResultEngine())
            == "TEXT_EXTRACTED"
        )
        assert (
            _mark_failure(str(document.uuid), "ocr_stale_failure") == "TEXT_EXTRACTED"
        )

    document.refresh_from_db()
    assert document.processing_status == MedicalDocument.ProcessingStatus.TEXT_EXTRACTED
    assert document.processing_failure_code == ""
    assert document.document_text.text == result().text


class MockResultEngine:
    def extract_image(self, image):
        return result()


def test_ocr_finishing_after_soft_delete_does_not_resurrect_document(tmp_path):
    require_postgresql()
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        document = make_document(tmp_path)
        engine = BlockingEngine()
        thread, results, failures = run_worker(str(document.uuid), engine)
        assert engine.started.wait(timeout=10)
        MedicalDocument.objects.filter(pk=document.pk).update(
            archive_status=MedicalDocument.ArchiveStatus.DELETED
        )
        finish(thread, engine)

    document.refresh_from_db()
    assert not failures
    assert results == ["SKIPPED"]
    assert document.archive_status == MedicalDocument.ArchiveStatus.DELETED
    assert not DocumentText.objects.filter(document=document).exists()


def test_new_m7_native_result_invalidates_stale_ocr_snapshot(tmp_path):
    require_postgresql()
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        document = prepare_pdf_document(tmp_path)
        old_text_uuid = document.document_text.uuid
        engine = BlockingEngine(result("stale OCR page two"))
        thread, results, failures = run_worker(
            str(document.uuid), engine, renderer=FakeRenderer()
        )
        assert engine.started.wait(timeout=10)
        replacement_text = "Replacement native page 1\n\f\nReplacement native page 2"
        extractor = Mock()
        extractor.extract.return_value = PDFTextResult(
            text=replacement_text,
            page_count=2,
            pages=(
                PDFTextPageResult(1, "Replacement native page 1", 24, False),
                PDFTextPageResult(2, "Replacement native page 2", 24, False),
            ),
            character_count=len(replacement_text),
            usable=True,
            reason="usable_text",
            metadata={
                **pdf_result(all_weak=False).metadata,
                "meaningful_character_count": 48,
                "pages_requiring_ocr": [],
            },
        )
        with patch("processing.tasks.ocr_medical_document.delay"):
            assert (
                process_pdf_document(
                    str(document.uuid), extractor=extractor, reprocess=True
                )
                == "TEXT_EXTRACTED"
            )
        finish(thread, engine)

    document.refresh_from_db()
    assert not failures
    assert results == ["TEXT_EXTRACTED"]
    assert document.document_text.uuid != old_text_uuid
    assert "stale OCR" not in document.document_text.text
    assert not document.document_text.pages.filter(ocr_completed=True).exists()
