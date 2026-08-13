import io
import math
import time
import warnings
from dataclasses import dataclass

from billiard.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from PIL import Image, ImageOps, UnidentifiedImageError

PREPROCESSING_VERSION = "m8-preprocess-v1"
OCR_PIPELINE_VERSION = "m8-ocr-v1"


class OCRError(Exception):
    code = "ocr_failed"
    retryable = False


class OCREngineUnavailableError(OCRError):
    code = "ocr_engine_unavailable"


class OCREngineResultError(OCRError):
    code = "ocr_malformed_result"


class OCRImageDecodeError(OCRError):
    code = "ocr_image_decode_failed"


class ImageDimensionLimitError(OCRError):
    code = "ocr_image_limit_exceeded"


class PDFPageRenderError(OCRError):
    code = "ocr_pdf_render_failed"


class OCRResultSizeError(OCRError):
    code = "ocr_text_limit_exceeded"


class OCRResourceError(OCRError):
    code = "ocr_resource_retryable"
    retryable = True


@dataclass(frozen=True)
class OCRLine:
    text: str
    confidence: float


@dataclass(frozen=True)
class OCRResult:
    text: str
    lines: tuple[OCRLine, ...]
    mean_confidence: float | None
    minimum_confidence: float | None
    engine_name: str
    engine_version: str
    duration_ms: int
    preprocessing_version: str = PREPROCESSING_VERSION
    pipeline_version: str = OCR_PIPELINE_VERSION


class OCREngine:
    def extract_image(self, image):
        raise NotImplementedError


def _validate_dimensions(width, height):
    if type(width) is not int or type(height) is not int or width <= 0 or height <= 0:
        raise ImageDimensionLimitError("Image dimensions are invalid.")
    if width > settings.OCR_MAX_WIDTH or height > settings.OCR_MAX_HEIGHT:
        raise ImageDimensionLimitError("Image dimensions exceed OCR limits.")
    if width * height > settings.OCR_MAX_IMAGE_PIXELS:
        raise ImageDimensionLimitError("Image pixel count exceeds OCR limits.")


class ImagePreprocessor:
    def prepare(self, content):
        if not isinstance(content, bytes) or not content:
            raise OCRImageDecodeError("Image content is malformed.")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(content)) as source:
                    _validate_dimensions(source.width, source.height)
                    source.load()
                    oriented = ImageOps.exif_transpose(source)
                    _validate_dimensions(oriented.width, oriented.height)
                    prepared = oriented.convert("RGB")
                    prepared.load()
                    return prepared
        except ImageDimensionLimitError:
            raise
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            UnidentifiedImageError,
            OSError,
            SyntaxError,
            ValueError,
        ) as exc:
            raise OCRImageDecodeError("Image content is malformed.") from exc


class PDFPageRenderer:
    def render(self, content, page_number):
        import pymupdf

        dpi = settings.OCR_PDF_RENDER_DPI
        if type(dpi) is not int or dpi <= 0 or dpi > settings.OCR_PDF_RENDER_MAX_DPI:
            raise PDFPageRenderError("PDF render DPI is invalid.")
        try:
            document = pymupdf.open(stream=content, filetype="pdf")
        except Exception as exc:
            raise PDFPageRenderError("PDF page cannot be rendered.") from exc
        try:
            if document.needs_pass or type(page_number) is not int:
                raise PDFPageRenderError("PDF page cannot be rendered.")
            if page_number < 1 or page_number > document.page_count:
                raise PDFPageRenderError("PDF page cannot be rendered.")
            page = document[page_number - 1]
            scale = dpi / 72
            projected_width = math.ceil(page.rect.width * scale)
            projected_height = math.ceil(page.rect.height * scale)
            _validate_dimensions(projected_width, projected_height)
            try:
                pixmap = page.get_pixmap(dpi=dpi, alpha=False)
                _validate_dimensions(pixmap.width, pixmap.height)
                image = Image.frombytes(
                    "RGB", (pixmap.width, pixmap.height), pixmap.samples
                )
                image.load()
                return image
            except ImageDimensionLimitError:
                raise
            except Exception as exc:
                raise PDFPageRenderError("PDF page cannot be rendered.") from exc
        finally:
            document.close()


class PaddleOCREngine(OCREngine):
    engine_name = "paddleocr"

    def __init__(
        self,
        *,
        pipeline=None,
        detection_model_name=None,
        recognition_model_name=None,
        detection_model_dir=None,
        recognition_model_dir=None,
    ):
        try:
            import paddleocr
        except ImportError as exc:
            raise OCREngineUnavailableError(
                "PaddleOCR runtime is unavailable."
            ) from exc
        self.engine_version = str(getattr(paddleocr, "__version__", "unknown"))
        self._requires_array_input = pipeline is None
        if pipeline is None:
            try:
                from paddleocr import PaddleOCR

                det_name = detection_model_name or settings.OCR_TEXT_DETECTION_MODEL_NAME
                rec_name = recognition_model_name or settings.OCR_TEXT_RECOGNITION_MODEL_NAME
                det_dir = detection_model_dir or settings.OCR_TEXT_DETECTION_MODEL_DIR
                rec_dir = recognition_model_dir or settings.OCR_TEXT_RECOGNITION_MODEL_DIR

                options = {
                    "device": "cpu",
                    "enable_mkldnn": False,
                    "use_doc_orientation_classify": False,
                    "use_doc_unwarping": False,
                    "use_textline_orientation": False,
                }
                if det_dir:
                    options["text_detection_model_dir"] = det_dir
                else:
                    options["text_detection_model_name"] = det_name
                if rec_dir:
                    options["text_recognition_model_dir"] = rec_dir
                else:
                    options["text_recognition_model_name"] = rec_name
                pipeline = PaddleOCR(**options)
            except Exception as exc:
                raise OCREngineUnavailableError(
                    "PaddleOCR runtime could not initialize."
                ) from exc
        self.pipeline = pipeline

    @staticmethod
    def _payload(item):
        payload = getattr(item, "json", item)
        if callable(payload):
            payload = payload()
        if not isinstance(payload, dict):
            raise OCREngineResultError("OCR engine returned malformed output.")
        payload = payload.get("res", payload)
        if not isinstance(payload, dict):
            raise OCREngineResultError("OCR engine returned malformed output.")
        return payload

    def extract_image(self, image):
        if not isinstance(image, Image.Image):
            raise OCRImageDecodeError("OCR input must be a decoded image.")
        started_at = time.monotonic()
        try:
            engine_input = image
            if self._requires_array_input:
                import numpy

                engine_input = numpy.asarray(image)
            raw_results = list(self.pipeline.predict(engine_input))
        except OCRError:
            raise
        except (MemoryError, OSError, SoftTimeLimitExceeded) as exc:
            raise OCRResourceError("OCR worker resource failure.") from exc
        except Exception as exc:
            raise OCRError("OCR engine execution failed.") from exc
        if len(raw_results) != 1:
            raise OCREngineResultError("OCR engine returned malformed output.")
        payload = self._payload(raw_results[0])
        texts = payload.get("rec_texts")
        scores = payload.get("rec_scores")
        if not isinstance(texts, (list, tuple)) or scores is None:
            raise OCREngineResultError("OCR engine returned malformed output.")
        try:
            scores = list(scores)
        except TypeError as exc:
            raise OCREngineResultError("OCR engine returned malformed output.") from exc
        if len(texts) != len(scores):
            raise OCREngineResultError("OCR engine returned malformed output.")

        lines = []
        for text, score in zip(texts, scores, strict=True):
            if not isinstance(text, str) or isinstance(score, bool):
                raise OCREngineResultError("OCR engine returned malformed output.")
            try:
                confidence = float(score)
            except (TypeError, ValueError) as exc:
                raise OCREngineResultError(
                    "OCR engine returned malformed output."
                ) from exc
            if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise OCREngineResultError("OCR engine returned malformed output.")
            if text:
                lines.append(OCRLine(text=text, confidence=confidence))

        text = "\n".join(line.text for line in lines)
        if len(text) > settings.OCR_MAX_TEXT_CHARS_PER_PAGE:
            raise OCRResultSizeError("OCR text exceeds the per-page limit.")
        confidences = [line.confidence for line in lines]
        return OCRResult(
            text=text,
            lines=tuple(lines),
            mean_confidence=(
                sum(confidences) / len(confidences) if confidences else None
            ),
            minimum_confidence=min(confidences) if confidences else None,
            engine_name=self.engine_name,
            engine_version=self.engine_version,
            duration_ms=int((time.monotonic() - started_at) * 1000),
        )
