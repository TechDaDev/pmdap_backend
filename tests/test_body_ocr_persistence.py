"""M17 medical-report body OCR persistence acceptance.

Proves that the full OCR body (not just metadata / dates) is persisted into
``DocumentText`` / ``DocumentTextPage`` and survives round-trips:

* long synthetic bodies are not truncated (TextField)
* synthetic lab-table bodies keep their TEST/RESULT/UNIT/REFERENCE tokens
* Unicode units symbols and Arabic/English/mixed script survive unchanged
* OCR success followed by date-parser failure keeps the body persisted

Uses synthetic text only (never owner medical data) through the production
OCR persistence path with a fake engine.
"""
import hashlib
import io
from datetime import date

import pytest
from django.core.files.base import ContentFile
from PIL import Image

from accounts.models import User
from documents.models import MedicalDocument, StoredFile
from patients.models import PatientProfile
from processing.date_services import process_date_candidates
from processing.models import DocumentText, DocumentTextPage
from processing.ocr_services import process_ocr_document
from tests.test_ocr_processing import FakeEngine, make_document, result

pytestmark = pytest.mark.django_db

# Synthetic sentinel table body. Never used on real medical data.
LAB_TABLE_BODY = "\n".join(
    [
        "Lab Report",
        "Item\tResult\tUnit\tReference Range",
        "Chemistry",
        "Glucose\t92\tmg/dL\t70-99",
        "Creatinine\t1.0\tmg/dL\t0.7-1.18",
        "HbA1c\t5.4\t%\t4.8-5.6",
        "Vitamin D3\t28\tng/mL\t30-100",
        "WBC\t6.7\tx10^3/µL\t4.0-11.0",
        "Platelets\t250\tx10^3/µL\t150-410",
        "Report Date: 14/03/2026",
    ]
)

ARABIC_LINE = "تقرير طبي مخبري"
MIXED_LINE = "Patient المريض 2026 ٢٠٢٦"
UNITS_LINE = "units: mg/dL ng/mL % µL μIU/ml"


def _long_body():
    # A body well past any 255 / 4k truncation boundary. No trailing newline:
    # the OCR adapter contract joins lines without a terminal separator.
    base = (
        "Synthetic long medical report body with repeating test rows. "
        "Haemoglobin 13.5 g/dL. WBC 6.5 x10^3/µL. Platelets 240 x10^3/µL. "
        "Reference 4.0-11.0. 0123456789 αβγ µ µL\n"
    )
    return (base * 120).rstrip("\n")  # ~ 15k chars


def test_long_body_persists_without_truncation(tmp_path):
    body = _long_body()
    document = make_document(tmp_path)

    outcome = process_ocr_document(
        str(document.uuid), engine=FakeEngine([result(body)])
    )

    assert outcome == MedicalDocument.ProcessingStatus.TEXT_EXTRACTED
    page = DocumentTextPage.objects.get(document_text__document=document)
    aggregate = DocumentText.objects.get(document=document)
    assert len(body) > 10_000
    assert aggregate.text == body
    assert aggregate.character_count == len(body)
    assert page.text == body
    assert page.text.count("\n") == body.count("\n")


def test_lab_table_tokens_survive_persistence(tmp_path):
    document = make_document(tmp_path)

    outcome = process_ocr_document(
        str(document.uuid), engine=FakeEngine([result(LAB_TABLE_BODY)])
    )

    document.refresh_from_db()
    page = document.document_text.pages.get()
    assert outcome == MedicalDocument.ProcessingStatus.TEXT_EXTRACTED
    # Future lab parsers need the test-name/result/unit/reference tokens.
    for token in (
        "Lab Report",
        "Item",
        "Result",
        "Unit",
        "Reference Range",
        "Chemistry",
        "Glucose",
        "92",
        "mg/dL",
        "70-99",
        "HbA1c",
        "5.4",
        "ng/mL",
        "30-100",
        "WBC",
        "6.7",
        "µL",
        "4.0-11.0",
        "Report Date: 14/03/2026",
    ):
        assert token in page.text
        assert token in document.document_text.text


def test_mixed_script_and_unit_symbols_roundtrip(tmp_path):
    body = f"{ARABIC_LINE}\n{MIXED_LINE}\n{UNITS_LINE}"
    document = make_document(tmp_path)

    process_ocr_document(str(document.uuid), engine=FakeEngine([result(body)]))

    page = DocumentTextPage.objects.get(document_text__document=document)
    assert ARABIC_LINE in page.text
    assert MIXED_LINE in page.text
    assert "mg/dL" in page.text
    assert "ng/mL" in page.text
    assert "%" in page.text
    assert "µL" in page.text
    assert "μIU/ml" in page.text
    # No mojibake: round-trip through UTF-8 is lossless.
    assert page.text.encode("utf-8").decode("utf-8") == page.text


def test_ocr_success_then_date_failure_keeps_body(tmp_path):
    """Full pipeline ordering: body persists BEFORE date detection; a broken
    date parser must not discard already-persisted canonical text."""
    document = make_document(tmp_path)
    process_ocr_document(str(document.uuid), engine=FakeEngine([result(LAB_TABLE_BODY)]))
    document.refresh_from_db()
    assert document.processing_status == MedicalDocument.ProcessingStatus.TEXT_EXTRACTED
    persisted_body = document.document_text.text

    def broken_detector(*args, **kwargs):
        del args, kwargs
        raise ValueError("private parser detail")

    outcome = process_date_candidates(str(document.uuid), detector=broken_detector)

    document.refresh_from_db()
    assert outcome == MedicalDocument.ProcessingStatus.FAILED
    assert document.processing_failure_code == "date_processing_failed"
    assert hasattr(document, "document_text")
    assert document.document_text.text == persisted_body
    assert document.document_text.pages.get().text == persisted_body
