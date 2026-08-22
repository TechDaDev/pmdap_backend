"""M23 — multi-page medical PDF as one source document with independent
page report units.

Covers: page-unit creation, ordering, independent statuses, page-scoped date
candidates + lab extraction, failure isolation, parent aggregation, confirm
count/list, cross-page span isolation, idempotency, delete, ownership/IDOR,
single-page regression, and the page API endpoints.
"""
from datetime import date
from unittest.mock import patch

import pytest

from documents.models import (
    MedicalDocument,
    MedicalDocumentEvent,
    MedicalDocumentPage,
)
from documents.page_services import (
    confirm_page_date,
    ensure_page_units,
    pending_page_units,
    recalculate_document_processing_state,
)
from labs.models import LabReportExtraction, LabResult
from processing.models import DateCandidate, DocumentText, DocumentTextPage, DocumentTextSpan
from tests.archive_helpers import make_document, verified_document
from tests.test_medical_documents_api import patient_user

pytestmark = pytest.mark.django_db

PAGES_URL = "/api/v1/documents/{uuid}/pages/"
PAGE_URL = "/api/v1/documents/{uuid}/pages/{page}/"
PAGE_LAB_URL = "/api/v1/documents/{uuid}/pages/{page}/lab-results/"
PAGE_CONFIRM_URL = "/api/v1/documents/{uuid}/pages/{page}/confirm-date/"
QUEUE = "/api/v1/documents/date-confirmations/pending/"


def attach_pages(document, page_rows):
    """Attach DocumentText + per-page grid spans (synthetic, parser-friendly).

    ``page_rows`` is a list of row-lists; each row is a list of cell strings.
    """
    flat_texts = ["\n".join(cell for row in rows for cell in row) for rows in page_rows]
    extracted = DocumentText.objects.create(
        document=document,
        text="\n\f\n".join(flat_texts),
        page_count=len(page_rows),
        character_count=sum(len(t) for t in flat_texts),
        meaningful_character_count=sum(len(t.replace(" ", "")) for t in flat_texts),
        usable=True,
        usability_reason="usable_ocr_text",
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
            native_text="",
            ocr_text="\n".join(cell for row in rows for cell in row),
            meaningful_character_count=sum(
                len(cell.replace(" ", "")) for row in rows for cell in row
            ),
            requires_ocr=False,
            ocr_completed=True,
            effective_source=DocumentTextPage.EffectiveSource.OCR,
        )
        sequence = 0
        cols = max((len(row) for row in rows), default=4)
        x_units = 1.0 / max(cols, 1)
        y_step = 1.0 / max(len(rows), 1)
        for ri, row in enumerate(rows):
            for ci, cell in enumerate(row):
                if not cell:
                    continue
                x0 = ci * x_units
                x1 = min(x0 + (x_units * 0.9), 1.0)
                y0 = ri * y_step
                y1 = min(y0 + (y_step * 0.8), 1.0)
                DocumentTextSpan.objects.create(
                    document_text_page=page,
                    sequence=sequence,
                    text=cell,
                    confidence=0.95,
                    x_min=x0,
                    y_min=y0,
                    x_max=x1,
                    y_max=y1,
                    source="OCR",
                    page_width=100,
                    page_height=100,
                )
                sequence += 1
    return extracted


CHEMISTRY_ROWS = [
    ["Item", "Result", "Unit", "Reference Range"],
    ["Chemistry"],
    ["Glucose", "92", "mg/dL", "70 - 99"],
    ["HbA1c", "5.4", "%", "Pre diabetic : 5.7-6.4"],
    ["Creatinine", "1.0", "mg/dL", "0.7 - 1.18"],
]

HORMONE_ROWS = [
    ["Item", "Result", "Unit", "Reference Range"],
    ["Vitamins"],
    ["Vitamin D3", "19.3", "ng/mL", "Sufficiency 29 - 100"],
    ["Hormones"],
    ["TSH", "1.24", "uIU/mL", "0.5 - 4.5"],
    ["T4", "8.3", "ug/dL", "4.9 - 11.0"],
]

CBC_ROWS = [
    ["Test", "Result", "Flags", "Units", "Low", "High"],
    ["CBC Complete Blood Count"],
    ["WBC", "6.71", "R", "x10^3/µL", "3.60", "10.20"],
    ["HGB", "14.68", "R", "g/dL", "12.50", "16.30"],
    ["PLT", "288.0", "R", "x10^3/µL", "150.0", "400.0"],
]


def make_three_page_doc(user, patient):
    document = make_document(patient, user, document_type="LABORATORY")
    attach_pages(document, [CHEMISTRY_ROWS, HORMONE_ROWS, CBC_ROWS])
    ensure_page_units(document, source_pages=[1, 2, 3])
    return document


# --------------------------------------------------------------------------- #
# Page-unit creation / ordering
# --------------------------------------------------------------------------- #


def test_three_page_pdf_creates_three_page_units():
    user, patient = patient_user()
    document = make_three_page_doc(user, patient)
    units = list(document.pages.order_by("page_number"))
    assert [u.page_number for u in units] == [1, 2, 3]
    assert document.pages.count() == 3
    assert all(
        u.processing_status == MedicalDocumentPage.ProcessingStatus.QUEUED
        for u in units
    )


def test_ensure_page_units_idempotent():
    user, patient = patient_user()
    document = make_document(patient, user)
    attach_pages(document, ["a"])
    ensure_page_units(document, source_pages=[1])
    assert document.pages.count() == 1
    ensure_page_units(document, source_pages=[1])
    assert document.pages.count() == 1


# --------------------------------------------------------------------------- #
# Independent statuses + parent aggregation
# --------------------------------------------------------------------------- #


def test_independent_page_statuses_and_parent_aggregation():
    user, patient = patient_user()
    document = make_three_page_doc(user, patient)
    p1, p2, p3 = list(document.pages.order_by("page_number"))

    p1.processing_status = MedicalDocumentPage.ProcessingStatus.READY
    p1.date_verified = True
    p1.save()
    p2.processing_status = MedicalDocumentPage.ProcessingStatus.AWAITING_CONFIRMATION
    p2.save()
    p3.processing_status = MedicalDocumentPage.ProcessingStatus.FAILED
    p3.save()
    parent = recalculate_document_processing_state(document)
    assert parent == MedicalDocument.ProcessingStatus.AWAITING_CONFIRMATION

    # All ready -> DATE_CONFIRMED
    for p in (p1, p2, p3):
        p.processing_status = MedicalDocumentPage.ProcessingStatus.READY
        p.date_verified = True
        p.document_date = date(2026, 3, 14)
        p.save()
    assert recalculate_document_processing_state(document) == (
        MedicalDocument.ProcessingStatus.DATE_CONFIRMED
    )
    document.refresh_from_db()
    assert document.document_date == date(2026, 3, 14)
    assert document.date_verified is True

    # Mixed ready + failed -> PARTIAL
    p3.processing_status = MedicalDocumentPage.ProcessingStatus.FAILED
    p3.save()
    assert recalculate_document_processing_state(document) == (
        MedicalDocument.ProcessingStatus.PARTIAL
    )

    # All failed -> FAILED
    for p in (p1, p2, p3):
        p.processing_status = MedicalDocumentPage.ProcessingStatus.FAILED
        p.save()
    assert recalculate_document_processing_state(document) == (
        MedicalDocument.ProcessingStatus.FAILED
    )


def test_parent_date_cleared_when_page_dates_differ():
    user, patient = patient_user()
    document = make_three_page_doc(user, patient)
    p1, p2, _ = list(document.pages.order_by("page_number"))
    for p, d in ((p1, date(2026, 1, 1)), (p2, date(2026, 2, 2))):
        p.processing_status = MedicalDocumentPage.ProcessingStatus.READY
        p.date_verified = True
        p.document_date = d
        p.save()
    recalculate_document_processing_state(document)
    document.refresh_from_db()
    assert document.document_date is None
    assert document.date_verified is False


# --------------------------------------------------------------------------- #
# Page-scoped date candidates
# --------------------------------------------------------------------------- #


def test_page_date_candidates_scoped_to_page():
    from processing.date_services import process_page_date_candidates

    user, patient = patient_user()
    document = make_three_page_doc(user, patient)
    p1 = document.pages.get(page_number=1)
    # override page 1 text to include a date
    p1text = "Biochemistry\nReport Date: 14/03/2026\nTest Result"
    page = document.document_text.pages.get(page_number=1)
    page.text = p1text
    page.save()
    process_page_date_candidates(str(p1.uuid))
    p1.refresh_from_db()
    cands = document.date_candidates.filter(page_unit=p1, is_current=True)
    assert cands.exists()
    assert all(c.page_unit_id == p1.pk for c in cands)
    assert all(c.page_number == 1 for c in cands)


def test_zero_candidate_page_still_finalizes():
    from processing.date_services import process_page_date_candidates
    from labs.services import process_lab_extraction_for_page

    user, patient = patient_user()
    document = make_document(patient, user, document_type="LABORATORY")
    attach_pages(document, [["no date here just text"]])
    ensure_page_units(document, source_pages=[1])
    p1 = document.pages.get(page_number=1)
    process_page_date_candidates(str(p1.uuid))
    p1.refresh_from_db()
    # date done, lab not yet -> still EXTRACTING
    assert p1.processing_status == MedicalDocumentPage.ProcessingStatus.EXTRACTING
    process_lab_extraction_for_page(str(p1.uuid))
    p1.refresh_from_db()
    assert p1.processing_status == MedicalDocumentPage.ProcessingStatus.AWAITING_CONFIRMATION


# --------------------------------------------------------------------------- #
# Page-scoped lab extraction + cross-page isolation
# --------------------------------------------------------------------------- #


def test_page_lab_extraction_scoped_and_span_isolated():
    from labs.services import process_lab_extraction_for_page

    user, patient = patient_user()
    document = make_three_page_doc(user, patient)
    p1 = document.pages.get(page_number=1)
    process_lab_extraction_for_page(str(p1.uuid))
    p1.refresh_from_db()
    ext = p1.lab_extractions.get()
    assert ext.status == LabReportExtraction.Status.COMPLETED
    rows = list(ext.results.all())
    assert rows
    assert all(r.page_number == 1 for r in rows)
    # every source span belongs to page 1 only
    for row in rows:
        span_pages = set(row.source_spans.values_list("document_text_page__page_number", flat=True))
        assert span_pages == {1}


def test_no_cross_page_contamination():
    from labs.services import process_lab_extraction_for_page

    user, patient = patient_user()
    document = make_three_page_doc(user, patient)
    for p in document.pages.order_by("page_number"):
        process_lab_extraction_for_page(str(p.uuid))
    for p in document.pages.order_by("page_number"):
        ext = p.lab_extractions.get()
        assert ext.status == LabReportExtraction.Status.COMPLETED
        pages = set(ext.results.values_list("page_number", flat=True))
        assert pages == {p.page_number}


def test_page_lab_failure_isolates_only_that_page():
    user, patient = patient_user()
    document = make_three_page_doc(user, patient)
    p1, p2, p3 = list(document.pages.order_by("page_number"))

    def boom(page_number, spans):
        if page_number == 3:
            raise RuntimeError("parser exploded")
        from labs.parsing import parse_page

        return parse_page(page_number, spans)

    from labs.services import process_lab_extraction_for_page

    process_lab_extraction_for_page(str(p1.uuid), parser=boom)
    process_lab_extraction_for_page(str(p2.uuid), parser=boom)
    process_lab_extraction_for_page(str(p3.uuid), parser=boom)

    p1.refresh_from_db()
    p2.refresh_from_db()
    p3.refresh_from_db()
    assert p1.processing_status == MedicalDocumentPage.ProcessingStatus.AWAITING_CONFIRMATION
    assert p2.processing_status == MedicalDocumentPage.ProcessingStatus.AWAITING_CONFIRMATION
    assert p3.processing_status == MedicalDocumentPage.ProcessingStatus.FAILED
    assert p1.lab_extractions.get().status == LabReportExtraction.Status.COMPLETED
    assert p2.lab_extractions.get().status == LabReportExtraction.Status.COMPLETED
    assert p3.lab_extractions.get().status == LabReportExtraction.Status.FAILED


# --------------------------------------------------------------------------- #
# Date confirmation count/list + confirm one page
# --------------------------------------------------------------------------- #


def test_pending_queue_counts_page_units_and_confirm_one():
    user, patient = patient_user()
    document = make_three_page_doc(user, patient)
    # finalize: date done (no candidates) + lab done -> AWAITING each page
    from processing.date_services import process_page_date_candidates
    from labs.services import process_lab_extraction_for_page

    for p in document.pages.order_by("page_number"):
        process_page_date_candidates(str(p.uuid))
        process_lab_extraction_for_page(str(p.uuid))

    pending = pending_page_units(patient)
    assert pending.count() == 3

    p1 = document.pages.get(page_number=1)
    confirm_page_date(
        page_unit=p1, actor=user, manual_date=date(2026, 3, 14)
    )
    assert pending_page_units(patient).count() == 2
    p1.refresh_from_db()
    assert p1.date_verified is True
    assert p1.processing_status == MedicalDocumentPage.ProcessingStatus.READY
    p2 = document.pages.get(page_number=2)
    assert p2.processing_status == MedicalDocumentPage.ProcessingStatus.AWAITING_CONFIRMATION
    document.refresh_from_db()
    assert document.processing_status == MedicalDocument.ProcessingStatus.AWAITING_CONFIRMATION

    # confirm remaining
    for p in document.pages.filter(date_verified=False):
        confirm_page_date(page_unit=p, actor=user, manual_date=date(2026, 3, 14))
    assert pending_page_units(patient).count() == 0
    document.refresh_from_db()
    assert document.processing_status == MedicalDocument.ProcessingStatus.DATE_CONFIRMED


# --------------------------------------------------------------------------- #
# Page API endpoints + ownership
# --------------------------------------------------------------------------- #


def test_page_api_summary(api_client):
    user, patient = patient_user()
    document = make_three_page_doc(user, patient)
    from labs.services import process_lab_extraction_for_page

    process_lab_extraction_for_page(str(document.pages.get(page_number=1).uuid))
    api_client.force_authenticate(user=user)
    response = api_client.get(PAGES_URL.format(uuid=document.uuid))
    assert response.status_code == 200
    data = response.data["data"]
    assert data["page_count"] == 3
    assert [p["page_number"] for p in data["pages"]] == [1, 2, 3]
    page1 = data["pages"][0]
    assert page1["lab_result_count"] >= 1
    assert "ocr" not in str(data).lower()


def test_page_api_detail_with_results(api_client):
    user, patient = patient_user()
    document = make_three_page_doc(user, patient)
    from labs.services import process_lab_extraction_for_page

    process_lab_extraction_for_page(str(document.pages.get(page_number=1).uuid))
    api_client.force_authenticate(user=user)
    response = api_client.get(PAGE_URL.format(uuid=document.uuid, page=1))
    assert response.status_code == 200
    data = response.data["data"]
    assert data["page_number"] == 1
    assert data["page_count"] == 3
    assert len(data["lab_results"]) == data["lab_result_count"]


def test_page_api_lab_results_scoped(api_client):
    user, patient = patient_user()
    document = make_three_page_doc(user, patient)
    from labs.services import process_lab_extraction_for_page

    process_lab_extraction_for_page(str(document.pages.get(page_number=2).uuid))
    api_client.force_authenticate(user=user)
    response = api_client.get(PAGE_LAB_URL.format(uuid=document.uuid, page=2))
    assert response.status_code == 200
    data = response.data["data"]
    assert data["page_number"] == 2
    for row in data["results"]:
        assert row["page_number"] == 2


def test_page_api_confirm_date(api_client):
    user, patient = patient_user()
    document = make_three_page_doc(user, patient)
    from processing.date_services import process_page_date_candidates
    from labs.services import process_lab_extraction_for_page

    for p in document.pages.order_by("page_number"):
        process_page_date_candidates(str(p.uuid))
        process_lab_extraction_for_page(str(p.uuid))
    api_client.force_authenticate(user=user)
    response = api_client.post(
        PAGE_CONFIRM_URL.format(uuid=document.uuid, page=1),
        {"date": "2026-03-14"},
        format="json",
    )
    assert response.status_code == 200
    data = response.data["data"]
    assert data["page_number"] == 1
    assert data["date_verified"] is True
    assert api_client.get(QUEUE).data["data"]["count"] == 2


def test_page_api_ownership_other_patient_404(api_client):
    user, patient = patient_user(email="owner@example.com", digital_id="11111111111111111")
    other_user, other_patient = patient_user(email="other@example.com", digital_id="76543210987654321")
    document = make_three_page_doc(other_user, other_patient)
    api_client.force_authenticate(user=user)
    assert api_client.get(PAGES_URL.format(uuid=document.uuid)).status_code == 404
    assert api_client.get(PAGE_URL.format(uuid=document.uuid, page=1)).status_code == 404
    assert (
        api_client.get(PAGE_LAB_URL.format(uuid=document.uuid, page=1)).status_code
        == 404
    )
    assert (
        api_client.post(
            PAGE_CONFIRM_URL.format(uuid=document.uuid, page=1),
            {"date": "2026-03-14"},
            format="json",
        ).status_code
        == 404
    )


def test_page_api_unknown_page_404(api_client):
    user, patient = patient_user()
    document = make_document(patient, user)
    attach_pages(document, [["one page"]])
    ensure_page_units(document, source_pages=[1])
    api_client.force_authenticate(user=user)
    assert (
        api_client.get(PAGE_URL.format(uuid=document.uuid, page=99)).status_code
        == 404
    )


# --------------------------------------------------------------------------- #
# Idempotency + delete + single-page regression
# --------------------------------------------------------------------------- #


def test_reprocess_page_lab_is_idempotent():
    from labs.services import process_lab_extraction_for_page

    user, patient = patient_user()
    document = make_three_page_doc(user, patient)
    p1 = document.pages.get(page_number=1)
    process_lab_extraction_for_page(str(p1.uuid))
    count1 = LabResult.objects.filter(extraction__page_unit=p1).count()
    process_lab_extraction_for_page(str(p1.uuid))
    count2 = LabResult.objects.filter(extraction__page_unit=p1).count()
    assert count1 == count2
    assert p1.lab_extractions.count() == 1


def test_delete_cascade_removes_children():
    from documents.services import purge_medical_document

    user, patient = patient_user()
    document = make_three_page_doc(user, patient)
    from labs.services import process_lab_extraction_for_page

    for p in document.pages.order_by("page_number"):
        process_lab_extraction_for_page(str(p.uuid))
    uuids = [p.uuid for p in document.pages.all()]
    extraction_uuids = list(
        LabReportExtraction.objects.filter(document=document).values_list("uuid", flat=True)
    )
    candidate_uuids = list(
        DateCandidate.objects.filter(document=document).values_list("uuid", flat=True)
    )
    purge_medical_document(document=document)
    assert MedicalDocumentPage.objects.filter(uuid__in=uuids).count() == 0
    assert LabReportExtraction.objects.filter(uuid__in=extraction_uuids).count() == 0
    assert DateCandidate.objects.filter(uuid__in=candidate_uuids).count() == 0


def test_single_page_document_regression(api_client):
    user, patient = patient_user()
    document = make_document(patient, user, document_type="LABORATORY")
    attach_pages(document, [CHEMISTRY_ROWS])
    ensure_page_units(document, source_pages=[1])
    p1 = document.pages.get(page_number=1)
    from processing.date_services import process_page_date_candidates
    from labs.services import process_lab_extraction_for_page

    process_page_date_candidates(str(p1.uuid))
    process_lab_extraction_for_page(str(p1.uuid))
    api_client.force_authenticate(user=user)
    # queue has exactly one entry
    assert api_client.get(QUEUE).data["data"]["count"] == 1
    response = api_client.get(PAGES_URL.format(uuid=document.uuid))
    assert response.data["data"]["page_count"] == 1
    assert len(response.data["data"]["pages"]) == 1
