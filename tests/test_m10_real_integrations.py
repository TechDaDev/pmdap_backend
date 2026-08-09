import io
import os

import pymupdf
import pytest
from PIL import Image, ImageDraw, ImageFont

from documents.date_services import confirm_document_date
from documents.models import MedicalDocument
from processing.date_services import process_date_candidates
from processing.extraction import PDFTextExtractor
from processing.models import DocumentTextPage
from processing.ocr import ImagePreprocessor, PaddleOCREngine
from tests.test_date_processing import prepared_document

pytestmark = pytest.mark.django_db


def _confirm_suggestion(document):
    assert (
        process_date_candidates(str(document.uuid))
        == MedicalDocument.ProcessingStatus.AWAITING_CONFIRMATION
    )
    candidate = document.date_candidates.get(is_current=True, is_suggested=True)
    result = confirm_document_date(
        document=document,
        actor=document.patient.user,
        candidate_id=candidate.uuid,
    )
    assert result.processing_status == MedicalDocument.ProcessingStatus.DATE_CONFIRMED
    assert result.date_verified is True
    assert result.date_source == MedicalDocument.DateSource.USER_CONFIRMED


def test_real_native_pdf_to_m9_to_m10():
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text(
        (60, 80),
        "Synthetic Medical Report - Report Date: 14/03/2026 - " * 3,
    )
    content = pdf.tobytes()
    pdf.close()

    extracted = PDFTextExtractor().extract(content)
    assert extracted.usable is True
    document = prepared_document(extracted.text)

    _confirm_suggestion(document)


@pytest.mark.real_ocr
@pytest.mark.skipif(
    os.getenv("PMDAP_RUN_REAL_OCR") != "1",
    reason="set PMDAP_RUN_REAL_OCR=1 for installed PaddleOCR acceptance",
)
def test_real_ocr_image_to_m9_to_m10():
    image = Image.new("RGB", (1600, 440), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 54)
    draw.text((80, 70), "Synthetic Medical Report", fill="black", font=font)
    draw.text((80, 220), "Report Date: 14/03/2026", fill="black", font=font)
    content = io.BytesIO()
    image.save(content, format="PNG")
    image.close()

    prepared = ImagePreprocessor().prepare(content.getvalue())
    try:
        ocr = PaddleOCREngine().extract_image(prepared)
    finally:
        prepared.close()
    document = prepared_document(
        ocr.text,
        source=DocumentTextPage.EffectiveSource.OCR,
    )

    _confirm_suggestion(document)
