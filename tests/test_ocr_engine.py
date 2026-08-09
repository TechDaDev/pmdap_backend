import hashlib
import io
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pymupdf
import pytest
from django.test import override_settings
from PIL import Image

from processing.ocr import (
    ImageDimensionLimitError,
    ImagePreprocessor,
    OCREngine,
    OCREngineResultError,
    OCRImageDecodeError,
    OCRResourceError,
    OCRResultSizeError,
    PaddleOCREngine,
    PDFPageRenderer,
    PDFPageRenderError,
)


def image_bytes(*, width=320, height=120, image_format="PNG"):
    image = Image.new("RGB", (width, height), "white")
    output = io.BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


class PaddleResult:
    def __init__(self, texts, scores):
        self.json = {"res": {"rec_texts": texts, "rec_scores": scores}}


def test_ocr_engine_contract_is_replaceable():
    with pytest.raises(NotImplementedError):
        OCREngine().extract_image(Image.new("RGB", (10, 10)))


def test_paddle_adapter_returns_structured_unicode_and_confidence(monkeypatch):
    monkeypatch.setitem(sys.modules, "paddleocr", SimpleNamespace(__version__="3.7.0"))
    pipeline = Mock()
    pipeline.predict.return_value = [
        PaddleResult(
            ["Medical Report", "تاريخ: ١٤/٠٣/٢٠٢٦", "عدد ۱۴۰۵"],
            [0.91, 0.83, 0.79],
        )
    ]
    result = PaddleOCREngine(pipeline=pipeline).extract_image(
        Image.new("RGB", (320, 120), "white")
    )

    assert result.text == "Medical Report\nتاريخ: ١٤/٠٣/٢٠٢٦\nعدد ۱۴۰۵"
    assert [line.text for line in result.lines] == [
        "Medical Report",
        "تاريخ: ١٤/٠٣/٢٠٢٦",
        "عدد ۱۴۰۵",
    ]
    assert result.mean_confidence == pytest.approx(0.843333, rel=1e-5)
    assert result.minimum_confidence == 0.79
    assert result.engine_name == "paddleocr"
    assert result.engine_version == "3.7.0"
    assert result.preprocessing_version == "m8-preprocess-v1"
    assert result.duration_ms >= 0
    pipeline.predict.assert_called_once()


@pytest.mark.parametrize(
    "payload",
    [
        [],
        [object()],
        [PaddleResult(["text"], [])],
        [PaddleResult([123], [0.9])],
        [PaddleResult(["text"], [1.5])],
    ],
)
def test_paddle_adapter_rejects_malformed_results(monkeypatch, payload):
    monkeypatch.setitem(sys.modules, "paddleocr", SimpleNamespace(__version__="3.7.0"))
    pipeline = Mock()
    pipeline.predict.return_value = payload
    with pytest.raises(OCREngineResultError):
        PaddleOCREngine(pipeline=pipeline).extract_image(
            Image.new("RGB", (100, 40), "white")
        )


def test_paddle_adapter_enforces_page_text_limit(monkeypatch):
    monkeypatch.setitem(sys.modules, "paddleocr", SimpleNamespace(__version__="3.7.0"))
    pipeline = Mock()
    pipeline.predict.return_value = [PaddleResult(["123456"], [0.9])]
    with override_settings(OCR_MAX_TEXT_CHARS_PER_PAGE=5):
        with pytest.raises(OCRResultSizeError):
            PaddleOCREngine(pipeline=pipeline).extract_image(
                Image.new("RGB", (100, 40), "white")
            )


def test_paddle_adapter_classifies_worker_resource_failure_as_retryable(monkeypatch):
    monkeypatch.setitem(sys.modules, "paddleocr", SimpleNamespace(__version__="3.7.0"))
    pipeline = Mock()
    pipeline.predict.side_effect = OSError("synthetic worker resource failure")
    with pytest.raises(OCRResourceError) as failure:
        PaddleOCREngine(pipeline=pipeline).extract_image(
            Image.new("RGB", (100, 40), "white")
        )
    assert failure.value.retryable is True


def test_preprocessing_preserves_original_and_returns_independent_rgb_image():
    content = image_bytes(image_format="JPEG")
    digest = hashlib.sha256(content).hexdigest()
    prepared = ImagePreprocessor().prepare(content)

    assert hashlib.sha256(content).hexdigest() == digest
    assert prepared.mode == "RGB"
    assert prepared.size == (320, 120)
    prepared.close()


@pytest.mark.parametrize(
    ("settings_values", "size"),
    [
        ({"OCR_MAX_WIDTH": 99}, (100, 40)),
        ({"OCR_MAX_HEIGHT": 39}, (100, 40)),
        ({"OCR_MAX_IMAGE_PIXELS": 3_999}, (100, 40)),
    ],
)
def test_preprocessing_rejects_dimension_and_pixel_limits(settings_values, size):
    with override_settings(**settings_values):
        with pytest.raises(ImageDimensionLimitError):
            ImagePreprocessor().prepare(image_bytes(width=size[0], height=size[1]))


def test_preprocessing_rejects_malformed_image():
    with pytest.raises(OCRImageDecodeError):
        ImagePreprocessor().prepare(b"not-an-image")


def test_preprocessing_rejects_pillow_decompression_bomb(monkeypatch):
    def bomb(*args, **kwargs):
        raise Image.DecompressionBombError("synthetic pixel bomb")

    monkeypatch.setattr(Image, "open", bomb)
    with pytest.raises(OCRImageDecodeError):
        ImagePreprocessor().prepare(image_bytes())


def test_pdf_renderer_renders_only_requested_page_at_configured_dpi():
    pdf = pymupdf.open()
    pdf.new_page(width=100, height=100)
    pdf.new_page(width=100, height=100)
    content = pdf.tobytes()
    pdf.close()

    with override_settings(OCR_PDF_RENDER_DPI=144):
        rendered = PDFPageRenderer().render(content, 2)
    assert rendered.size == (200, 200)
    rendered.close()


def test_pdf_renderer_rejects_invalid_page_and_projected_pixel_bomb():
    pdf = pymupdf.open()
    pdf.new_page(width=1000, height=1000)
    content = pdf.tobytes()
    pdf.close()

    with pytest.raises(PDFPageRenderError):
        PDFPageRenderer().render(content, 2)
    with override_settings(OCR_PDF_RENDER_DPI=300, OCR_MAX_IMAGE_PIXELS=10_000):
        with pytest.raises(ImageDimensionLimitError):
            PDFPageRenderer().render(content, 1)
