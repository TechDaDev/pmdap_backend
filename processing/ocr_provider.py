"""Per-worker-process OCR engine provider.

PaddleOCR pipeline construction (model loading) is expensive. Celery's prefork
pool runs one task at a time inside each ForkPoolWorker OS process, so a single
lazily-created engine can be safely reused across all tasks in that process.

Guarantees:
  * lazy — the first task in a process constructs the engine once
  * no cross-process sharing — each ForkPoolWorker holds its own module global
  * no thread sharing — tasks run sequentially per ForkPoolWorker; the Django
    web (gunicorn) process never imports this module
  * failed initialization does not poison the process — on construction error
    ``_engine`` stays ``None`` and the next task retries construction
  * no document/image state is retained — only the stateless PaddleOCR
    pipeline object is cached
  * tests can inject/reset via [set_ocr_engine]/[reset_ocr_engine]
"""
from __future__ import annotations

from django.conf import settings

from processing.ocr import PaddleOCREngine

_engine = None
_created_count = 0
_latin_engine = None
_latin_created_count = 0


def get_ocr_engine():
    """Return the per-process OCR engine, constructing it lazily once.

    Raises the same OCREngineUnavailableError as PaddleOCREngine on failure;
    the cache stays empty so the next call retries construction.
    """
    global _engine, _created_count
    if _engine is None:
        _engine = PaddleOCREngine()
        _created_count += 1
    return _engine


def get_latin_ocr_engine():
    """Return a per-process Latin/multilingual OCR engine (targeted ROI reads).

    Built from the OCR_LATIN_* settings so the worker image can preload the
    Latin recognizer. Constructed lazily once per worker process and reused,
    matching the Arabic engine lifecycle. On failure the cache stays empty so
    the next call retries construction.
    """
    global _latin_engine, _latin_created_count
    if _latin_engine is None:
        _latin_engine = PaddleOCREngine(
            detection_model_name=settings.OCR_LATIN_DETECTION_MODEL_NAME,
            recognition_model_name=settings.OCR_LATIN_RECOGNITION_MODEL_NAME,
            detection_model_dir=settings.OCR_LATIN_DETECTION_MODEL_DIR,
            recognition_model_dir=settings.OCR_LATIN_RECOGNITION_MODEL_DIR,
        )
        _latin_created_count += 1
    return _latin_engine


def engine_created_count() -> int:
    """How many times this OS process has constructed an engine (0, 1, ...)."""
    return _created_count


def latin_engine_created_count() -> int:
    """How many times the Latin engine was constructed in this process."""
    return _latin_created_count


def reset_ocr_engine():
    """Drop the cached Arabic engine (tests, or a deliberate engine reset)."""
    global _engine, _created_count
    _engine = None
    _created_count = 0


def reset_latin_ocr_engine():
    """Drop the cached Latin engine (tests, or a deliberate engine reset)."""
    global _latin_engine, _latin_created_count
    _latin_engine = None
    _latin_created_count = 0


def set_ocr_engine(engine):
    """Inject an Arabic engine (tests only)."""
    global _engine
    _engine = engine


def set_latin_ocr_engine(engine):
    """Inject a Latin engine (tests only)."""
    global _latin_engine
    _latin_engine = engine
