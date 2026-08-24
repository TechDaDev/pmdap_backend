"""M25 — page-scoped lab results + page date confirmation.

Covers: page extraction persists page_unit FK, subtype classification
(chemistry/hormones/CBC), page-scoped lab-results endpoint, lazy OCR when a
native-text page has no spans, page confirm-date (one leaves others pending,
all aggregate parent + shared date), cross-page evidence isolation, legacy
single-page behavior.
"""
from datetime import date
from unittest.mock import patch

import pytest

from django.conf import settings

from documents.models import MedicalDocumentPage
from documents.page_services import (
    detect_report_subtype,
    pending_page_units,
    recalculate_document_processing_state,
)
from labs.models import LabReportExtraction, LabResult
from processing.models import DateCandidate, DocumentText, DocumentTextPage, DocumentTextSpan
from tests.archive_helpers import make_document
from tests.test_medical_documents_api import patient_user

pytestmark = pytest.mark.django_db

PAGE_URL = "/api/v1/documents/{uuid}/pages/{page}/"
PAGE_LAB_URL = "/api/v1/documents/{uuid}/pages/{page}/lab-results/"
PAGE_CONFIRM_URL = "/api/v1/documents/{uuid}/pages/{page}/confirm-date/"
QUEUE = "/api/v1/documents/date-confirmations/pending/"


# --------------------------------------------------------------------------- #
# Subtype classification (pure)
# --------------------------------------------------------------------------- #


def test_subtype_chemistry():
    assert (
        detect_report_subtype("Biochemistry\nGlucose 92 mg/dL\nCreatinine 1.0")
        == "LAB_CHEMISTRY"
    )


def test_subtype_hormones():
    assert (
        detect_report_subtype("Vitamins\nVitamin D3 19.3\nTSH 1.24")
        == "LAB_HORMONES"
    )


def test_subtype_cbc():
    assert (
        detect_report_subtype("CBC\nWBC 6.7\nHGB 14.6\nPLT 288")
        == "LAB_CBC"
    )


def test_shared_chemistry_header_does_not_dominate_cbc():
    # "chemistry" appears in a shared header but CBC analyte cues win.
    text = "ISO 15189:2012 Chemistry\nComplete Blood Count\nWBC 6.7 R x10^3/uL"
    assert detect_report_subtype(text) == "LAB_CBC"


def test_shared_chemistry_header_does_not_dominate_hormones():
    text = "Laboratory Chemistry\nVitamins\nVitamin D3 19.3 ng/mL\nTSH 1.24"
    assert detect_report_subtype(text) == "LAB_HORMONES"


# --------------------------------------------------------------------------- #
# Page lab extraction ownership + scoping
# --------------------------------------------------------------------------- #


def attach_grid(document, page_rows):
    """Grid spans (like M23) with one page per row-list."""
    extracted = DocumentText.objects.create(
        document=document,
        text="",
        page_count=len(page_rows),
        character_count=1,
        meaningful_character_count=1,
        usable=True,
        usability_reason="u",
        has_pages_requiring_ocr=False,
        extraction_method=DocumentText.ExtractionMethod.OCR,
        extractor_name="fake",
        extractor_version="1",
        pipeline_version="test",
    )
    for number, rows in enumerate(page_rows, start=1):
        page = DocumentTextPage.objects.create(
            document_text=extracted,
            page_number=number,
            text="\n".join(cell for row in rows for cell in row),
            ocr_text="",
            meaningful_character_count=1,
            requires_ocr=False,
            ocr_completed=True,
            effective_source=DocumentTextPage.EffectiveSource.OCR,
        )
        cols = max((len(row) for row in rows), default=4)
        x_units = 1.0 / max(cols, 1)
        y_step = 1.0 / max(len(rows), 1)
        seq = 0
        for ri, row in enumerate(rows):
            for ci, cell in enumerate(row):
                if not cell:
                    continue
                DocumentTextSpan.objects.create(
                    document_text_page=page,
                    sequence=seq,
                    text=cell,
                    confidence=0.95,
                    x_min=ci * x_units,
                    y_min=ri * y_step,
                    x_max=min(ci * x_units + x_units * 0.9, 1.0),
                    y_max=min(ri * y_step + y_step * 0.8, 1.0),
                    source="OCR",
                    page_width=100,
                    page_height=100,
                )
                seq += 1
    return extracted


CHEM = [["Item", "Result", "Unit", "Reference"], ["Glucose", "92", "mg/dL", "70-99"]]
HORM = [["Item", "Result", "Unit", "Reference"], ["TSH", "1.24", "uIU/mL", "0.5-4.5"]]
CBC = [["Test", "Result"], ["WBC", "6.7"], ["HGB", "14.6"]]


def make_pdf(user, patient):
    document = make_document(patient, user, document_type="LABORATORY")
    attach_grid(document, [CHEM, HORM, CBC])
    from documents.page_services import ensure_page_units

    ensure_page_units(document, source_pages=[1, 2, 3])
    return document


def test_page_extraction_persists_page_unit_and_rows():
    from labs.services import process_lab_extraction_for_page

    user, patient = patient_user()
    document = make_pdf(user, patient)
    for p in document.pages.order_by("page_number"):
        process_lab_extraction_for_page(str(p.uuid))
        p.refresh_from_db()
        ext = LabReportExtraction.objects.filter(page_unit=p).first()
        assert ext is not None
        assert ext.page_unit_id == p.pk
        assert ext.status == "COMPLETED"
        rows = LabResult.objects.filter(extraction=ext)
        assert rows.count() >= 1
        # cross-page evidence isolation
        for r in rows:
            span_pages = set(
                r.source_spans.values_list("document_text_page__page_number", flat=True)
            )
            assert span_pages == {p.page_number}


def test_page_lab_results_endpoint_scoped(api_client):
    from labs.services import process_lab_extraction_for_page

    user, patient = patient_user()
    document = make_pdf(user, patient)
    process_lab_extraction_for_page(str(document.pages.get(page_number=1).uuid))
    process_lab_extraction_for_page(str(document.pages.get(page_number=2).uuid))
    api_client.force_authenticate(user=user)
    r1 = api_client.get(PAGE_LAB_URL.format(uuid=document.uuid, page=1))
    r2 = api_client.get(PAGE_LAB_URL.format(uuid=document.uuid, page=2))
    assert r1.status_code == 200
    assert r2.status_code == 200
    d1 = r1.data["data"]
    d2 = r2.data["data"]
    assert d1["page_number"] == 1 and d2["page_number"] == 2
    for row in d1["results"]:
        assert row["page_number"] == 1
    for row in d2["results"]:
        assert row["page_number"] == 2


# --------------------------------------------------------------------------- #
# Lazy OCR when native-text page has no spans
# --------------------------------------------------------------------------- #


def test_lazy_ocr_produces_spans_when_native_text_has_none(api_client):
    from labs.services import process_lab_extraction_for_page
    from processing.ocr_services import _persist_image_result  # noqa: F401

    user, patient = patient_user()
    document = make_document(patient, user, document_type="LABORATORY")
    # native-text page, no OCR, no spans (scanned PDF scenario)
    extracted = DocumentText.objects.create(
        document=document,
        text="Glucose 92 mg/dL",
        page_count=1,
        character_count=16,
        meaningful_character_count=14,
        usable=True,
        usability_reason="usable_pdf_text",
        has_pages_requiring_ocr=False,
        extraction_method=DocumentText.ExtractionMethod.PDF_TEXT,
        extractor_name="fake",
        extractor_version="1",
        pipeline_version="test",
    )
    DocumentTextPage.objects.create(
        document_text=extracted,
        page_number=1,
        text="Glucose 92 mg/dL",
        native_text="Glucose 92 mg/dL",
        ocr_text="",
        meaningful_character_count=14,
        requires_ocr=False,
        ocr_completed=False,
        effective_source=DocumentTextPage.EffectiveSource.PDF_TEXT,
    )
    from documents.page_services import ensure_page_units

    ensure_page_units(document, source_pages=[1])

    # Fake OCR: render -> result with a single span line.
    class _FakeResult:
        text = "Glucose 92 mg/dL"
        lines = ()
        engine_name = "fake"
        engine_version = "1"
        duration_ms = 10
        preprocessing_version = "v"
        pipeline_version = "v"
        mean_confidence = None
        minimum_confidence = None

    class _FakeLine:
        text = "Glucose 92 mg/dL"
        confidence = 0.9
        x_min = 10
        y_min = 10
        x_max = 300
        y_max = 20

    _FakeResult.lines = (_FakeLine(),)

    class _FakeImage:
        size = (320, 120)

        def close(self):
            pass

    class _FakeRenderer:
        def render(self, content, page_number):
            return _FakeImage()

    class _FakeEngine:
        def extract_image(self, image):
            return _FakeResult()

    with (
        patch("processing.services._read_verified_content", return_value=(b"x", None)),
        patch(
            "processing.ocr_provider.get_ocr_engine", return_value=_FakeEngine()
        ),
        patch("processing.ocr.PDFPageRenderer", return_value=_FakeRenderer()),
    ):
        p1 = document.pages.get(page_number=1)
        process_lab_extraction_for_page(str(p1.uuid))

    page = document.document_text.pages.get(page_number=1)
    assert page.spans.exists()
    assert page.ocr_completed is True
    assert page.effective_source == "OCR"


# --------------------------------------------------------------------------- #
# Page date confirmation semantics
# --------------------------------------------------------------------------- #


def finalize_pages(document):
    from labs.services import process_lab_extraction_for_page
    from processing.date_services import process_page_date_candidates

    for p in document.pages.order_by("page_number"):
        process_page_date_candidates(str(p.uuid))
        process_lab_extraction_for_page(str(p.uuid))


def test_confirm_one_leaves_others_pending(api_client):
    from documents.page_services import confirm_page_date

    user, patient = patient_user()
    document = make_pdf(user, patient)
    finalize_pages(document)
    assert pending_page_units(patient).count() == 3

    p1 = document.pages.get(page_number=1)
    confirm_page_date(page_unit=p1, actor=user, manual_date=date(2026, 6, 30))
    assert pending_page_units(patient).count() == 2
    p1.refresh_from_db()
    assert p1.date_verified is True
    assert p1.processing_status == "READY"
    assert document.pages.get(page_number=2).processing_status == (
        "AWAITING_CONFIRMATION"
    )
    assert document.pages.get(page_number=3).processing_status == (
        "AWAITING_CONFIRMATION"
    )
    document.refresh_from_db()
    assert document.processing_status == "AWAITING_CONFIRMATION"


def test_confirm_all_aggregates_parent_shared_date():
    from documents.page_services import confirm_page_date

    user, patient = patient_user()
    document = make_pdf(user, patient)
    finalize_pages(document)
    for p in document.pages.order_by("page_number"):
        confirm_page_date(page_unit=p, actor=user, manual_date=date(2026, 6, 30))
    assert pending_page_units(patient).count() == 0
    document.refresh_from_db()
    assert document.processing_status == "DATE_CONFIRMED"
    assert document.document_date == date(2026, 6, 30)
    assert document.date_verified is True


def test_page_confirm_endpoint_only_affects_page(api_client):
    user, patient = patient_user()
    document = make_pdf(user, patient)
    finalize_pages(document)
    api_client.force_authenticate(user=user)
    r = api_client.post(
        PAGE_CONFIRM_URL.format(uuid=document.uuid, page=1),
        {"date": "2026-06-30"},
        format="json",
    )
    assert r.status_code == 200
    assert r.data["data"]["page_number"] == 1
    assert r.data["data"]["date_verified"] is True
    assert api_client.get(QUEUE).data["data"]["count"] == 2


def test_page_detail_shows_status_and_candidates(api_client):
    user, patient = patient_user()
    document = make_pdf(user, patient)
    finalize_pages(document)
    api_client.force_authenticate(user=user)
    r = api_client.get(PAGE_URL.format(uuid=document.uuid, page=2))
    assert r.status_code == 200
    data = r.data["data"]
    assert data["page_number"] == 2
    assert data["processing_status"] == "AWAITING_CONFIRMATION"
    assert data["report_subtype"] == "LAB_HORMONES"
    assert data["lab_result_count"] >= 1
    assert len(data["lab_results"]) == data["lab_result_count"]


def test_legacy_single_page_extraction_still_document_level():
    # Single-page docs keep the document-level extraction (page_unit null) so
    # the existing document lab-results endpoint stays valid.
    user, patient = patient_user()
    document = make_document(patient, user, document_type="LABORATORY")
    attach_grid(document, [CHEM])
    from documents.page_services import ensure_page_units

    ensure_page_units(document, source_pages=[1])
    from labs.services import process_lab_extraction

    process_lab_extraction(str(document.uuid))
    ext = LabReportExtraction.objects.filter(document=document).first()
    assert ext is not None
    assert ext.page_unit is None
    assert ext.status == "COMPLETED"
