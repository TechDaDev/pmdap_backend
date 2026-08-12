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

from processing.ocr import PaddleOCREngine

_engine = None
_created_count = 0


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


def engine_created_count() -> int:
    """How many times this OS process has constructed an engine (0, 1, ...)."""
    return _created_count


def reset_ocr_engine():
    """Drop the cached engine (tests, or a deliberate engine reset)."""
    global _engine, _created_count
    _engine = None
    _created_count = 0


def set_ocr_engine(engine):
    """Inject an engine (tests only)."""
    global _engine
    _engine = engine
