from unittest.mock import patch

import pytest
from django.test import override_settings

from processing.extraction import (
    PDFEncryptedError,
    PDFExtractionError,
    PDFPageLimitError,
    PDFTextExtractor,
    TextUsabilityEvaluator,
)

pymupdf = pytest.importorskip("pymupdf")


def pdf_bytes(*pages):
    document = pymupdf.open()
    for text in pages:
        page = document.new_page()
        if text:
            page.insert_htmlbox(
                pymupdf.Rect(36, 36, 560, 800),
                f"<p>{text}</p>",
            )
    content = document.tobytes()
    document.close()
    return content


def encrypted_pdf_bytes(text="Confidential digital report " * 5):
    document = pymupdf.open(stream=pdf_bytes(text), filetype="pdf")
    content = document.tobytes(
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw="owner-secret",
        user_pw="user-secret",
    )
    document.close()
    return content


@override_settings(
    PDF_TEXT_MIN_CHARS=20,
    PDF_TEXT_MIN_PAGE_CHARS=10,
    PDF_TEXT_MIN_TEXT_PAGE_RATIO=0.5,
)
def test_extracts_single_page_with_structured_provenance():
    source = "Digital clinical report with enough meaningful content 12345."

    result = PDFTextExtractor().extract(pdf_bytes(source))

    assert source in result.text
    assert result.page_count == 1
    assert len(result.pages) == 1
    assert result.pages[0].page_number == 1
    assert source in result.pages[0].text
    assert result.character_count == len(result.text)
    assert result.usable is True
    assert result.reason == "usable_pdf_text"
    assert result.metadata["extraction_method"] == "PDF_TEXT"
    assert result.metadata["extractor_name"] == "PyMuPDF"
    assert result.metadata["extractor_version"] == pymupdf.__version__
    assert result.metadata["pipeline_version"] == "m7-v1"


@override_settings(
    PDF_TEXT_MIN_CHARS=20,
    PDF_TEXT_MIN_PAGE_CHARS=10,
    PDF_TEXT_MIN_TEXT_PAGE_RATIO=0.5,
)
def test_preserves_page_order_boundaries_and_unicode():
    english = "English medical summary 12345"
    arabic = "تقرير طبي عراقي ١٢٣٤٥"
    mixed = "Patient المريض 2026 ٢٠٢٦"

    content = pdf_bytes(english, arabic, mixed)
    source = pymupdf.open(stream=content, filetype="pdf")
    raw_pages = tuple(page.get_text("text") for page in source)
    source.close()

    result = PDFTextExtractor().extract(content)

    assert [page.page_number for page in result.pages] == [1, 2, 3]
    assert tuple(page.text for page in result.pages) == raw_pages
    assert english in raw_pages[0]
    assert any("\u0600" <= character <= "\u06ff" for character in raw_pages[1])
    assert any(character in "٠١٢٣٤٥٦٧٨٩" for character in raw_pages[1])
    assert "Patient" in raw_pages[2]
    assert result.text.index(raw_pages[0]) < result.text.index(raw_pages[1])
    assert result.text.index(raw_pages[1]) < result.text.index(raw_pages[2])
    assert "\f" in result.text


@pytest.mark.parametrize("junk", ["1", ".", "scan"])
@override_settings(
    PDF_TEXT_MIN_CHARS=20,
    PDF_TEXT_MIN_PAGE_CHARS=10,
    PDF_TEXT_MIN_TEXT_PAGE_RATIO=0.5,
)
def test_short_or_punctuation_junk_is_not_usable(junk):
    result = PDFTextExtractor().extract(pdf_bytes(junk))

    assert result.usable is False
    assert result.reason == "insufficient_meaningful_text"
    assert result.pages[0].requires_ocr is True


@override_settings(
    PDF_TEXT_MIN_CHARS=20,
    PDF_TEXT_MIN_PAGE_CHARS=10,
    PDF_TEXT_MIN_TEXT_PAGE_RATIO=0.5,
)
def test_image_only_pdf_is_ocr_required_without_running_ocr():
    result = PDFTextExtractor().extract(pdf_bytes(""))

    assert result.text == ""
    assert result.character_count == 0
    assert result.usable is False
    assert result.reason == "insufficient_meaningful_text"
    assert result.pages[0].requires_ocr is True


@override_settings(
    PDF_TEXT_MIN_CHARS=20,
    PDF_TEXT_MIN_PAGE_CHARS=10,
    PDF_TEXT_MIN_TEXT_PAGE_RATIO=0.5,
)
def test_mixed_pdf_preserves_digital_text_and_marks_only_weak_page():
    first = "First digital page has meaningful clinical text 12345"
    third = "Third digital page also has useful discharge text 67890"

    result = PDFTextExtractor().extract(pdf_bytes(first, "scan", third))

    assert result.usable is True
    assert [page.requires_ocr for page in result.pages] == [False, True, False]
    assert first in result.text and third in result.text
    assert result.metadata["pages_requiring_ocr"] == [2]


def test_usability_evaluator_counts_unicode_alphanumerics_only():
    evaluator = TextUsabilityEvaluator(
        min_chars=4,
        min_page_chars=3,
        min_text_page_ratio=0.5,
    )

    result = evaluator.evaluate((" .. أ١ب2 ", "..."))

    assert result.total_meaningful_characters == 4
    assert result.page_meaningful_characters == (4, 0)
    assert result.page_requires_ocr == (False, True)
    assert result.usable is True


def test_encrypted_pdf_is_rejected_without_password_bypass():
    with pytest.raises(PDFEncryptedError) as exc_info:
        PDFTextExtractor().extract(encrypted_pdf_bytes())

    assert exc_info.value.code == "pdf_encrypted"
    assert exc_info.value.retryable is False


@override_settings(PDF_MAX_PAGES=2)
def test_page_limit_is_enforced_before_page_extraction():
    with pytest.raises(PDFPageLimitError) as exc_info:
        PDFTextExtractor().extract(pdf_bytes("one", "two", "three"))

    assert exc_info.value.code == "pdf_page_limit_exceeded"
    assert exc_info.value.retryable is False


def test_malformed_pdf_and_page_extraction_exception_are_controlled():
    with pytest.raises(PDFExtractionError) as malformed:
        PDFTextExtractor().extract(b"not-a-pdf")
    assert malformed.value.code == "pdf_extraction_failed"

    with (
        patch.object(
            pymupdf.Page,
            "get_text",
            side_effect=RuntimeError("internal parser details"),
        ),
        pytest.raises(PDFExtractionError) as parser_failure,
    ):
        PDFTextExtractor().extract(pdf_bytes("Digital text"))
    assert parser_failure.value.code == "pdf_extraction_failed"

    with (
        patch.object(
            pymupdf,
            "open",
            side_effect=RuntimeError("unexpected open failure"),
        ),
        pytest.raises(PDFExtractionError) as open_failure,
    ):
        PDFTextExtractor().extract(b"%PDF-1.7")
    assert open_failure.value.code == "pdf_extraction_failed"
