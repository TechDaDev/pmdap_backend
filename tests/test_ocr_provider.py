"""Per-worker OCR engine provider tests.

The provider caches one PaddleOCR engine per OS process. These tests verify
lazy creation, reuse, retry-after-failure, reset/injection hooks and that the
identity extraction task reuses the same engine across jobs in one process.
"""
import io

import pytest
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image
from rest_framework_simplejwt.tokens import RefreshToken

from identities.models import IdentityExtractionJob
from identities.storage import private_identity_storage
from processing.ocr import OCREngineUnavailableError
from processing.ocr_provider import (
    engine_created_count,
    get_latin_ocr_engine,
    get_ocr_engine,
    latin_engine_created_count,
    reset_latin_ocr_engine,
    reset_ocr_engine,
    set_ocr_engine,
)
from tests.factories import UserFactory


@pytest.fixture(autouse=True)
def reset_engine_cache():
    reset_ocr_engine()
    reset_latin_ocr_engine()
    _CountingEngine.constructions = 0
    yield
    reset_ocr_engine()
    reset_latin_ocr_engine()


@pytest.fixture(autouse=True)
def identity_storage_dir(settings, tmp_path):
    settings.IDENTITY_FILE_ROOT = tmp_path / "private-identity"
    settings.IDENTITY_FILE_MAX_BYTES = 1024 * 1024


class _CountingEngine:
    constructions = 0

    def __init__(self):
        type(self).constructions += 1

    def extract_image(self, image):
        return None


def test_lazy_creation_happens_once(monkeypatch):
    monkeypatch.setattr("processing.ocr_provider.PaddleOCREngine", _CountingEngine)
    first = get_ocr_engine()
    second = get_ocr_engine()
    assert first is second
    assert _CountingEngine.constructions == 1
    assert engine_created_count() == 1


def test_failed_init_can_retry(monkeypatch):
    state = {"fail": True}

    class FlakyEngine:
        def __init__(self):
            if state["fail"]:
                raise OCREngineUnavailableError("paddle unavailable")

    monkeypatch.setattr("processing.ocr_provider.PaddleOCREngine", FlakyEngine)
    with pytest.raises(OCREngineUnavailableError):
        get_ocr_engine()
    # Cache is empty after failure → next call retries and succeeds.
    state["fail"] = False
    engine = get_ocr_engine()
    assert engine is not None
    assert engine_created_count() == 1


def test_reset_drops_cached_engine(monkeypatch):
    monkeypatch.setattr("processing.ocr_provider.PaddleOCREngine", _CountingEngine)
    get_ocr_engine()
    reset_ocr_engine()
    get_ocr_engine()
    assert _CountingEngine.constructions == 2


def test_injection_hook(monkeypatch):
    injected = _CountingEngine()
    set_ocr_engine(injected)
    assert get_ocr_engine() is injected
    # Injected engine is not counted as a construction.
    assert engine_created_count() == 0


def synthetic_png():
    out = io.BytesIO()
    Image.new("RGB", (8, 8), color=(35, 80, 120)).save(out, format="PNG")
    out.seek(0)
    return SimpleUploadedFile("syn.png", out.getvalue(), content_type="image/png")


def _auth(api_client, user):
    api_client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}"
    )


def _pending_job_with_staging(user):
    job = IdentityExtractionJob.objects.create(
        user=user,
        document_type="UNIFIED_NATIONAL_CARD",
        status=IdentityExtractionJob.Status.PENDING,
    )
    front_key = f"extract_staging/{job.uuid}/front.png"
    back_key = f"extract_staging/{job.uuid}/back.png"
    raw = synthetic_png().read()
    private_identity_storage.save(front_key, ContentFile(raw))
    private_identity_storage.save(back_key, ContentFile(raw))
    job.front_key = front_key
    job.back_key = back_key
    job.save(update_fields=["front_key", "back_key"])
    return job


@pytest.mark.django_db
@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
def test_identity_task_reuses_same_engine_across_two_jobs(monkeypatch):
    from identities.tasks import extract_identity_document

    class FakeEngine:
        constructions = 0

        def __init__(self, *args, **kwargs):
            type(self).constructions += 1

        def extract_image(self, image):
            return type(
                "R",
                (),
                {"lines": (type("L", (), {"text": "x", "confidence": 0.99})(),)},
            )()

    monkeypatch.setattr("processing.ocr_provider.PaddleOCREngine", FakeEngine)
    user = UserFactory(status="ACTIVE")

    job1 = _pending_job_with_staging(user)
    job2 = _pending_job_with_staging(user)

    extract_identity_document(str(job1.uuid))
    extract_identity_document(str(job2.uuid))

    job1.refresh_from_db()
    job2.refresh_from_db()
    assert job1.status == IdentityExtractionJob.Status.SUCCESS
    assert job2.status == IdentityExtractionJob.Status.SUCCESS
    # Two per-process singletons (Arabic + Latin), each constructed ONCE and
    # reused across all images of both jobs — per-process engine reuse works.
    assert FakeEngine.constructions == 2
    assert engine_created_count() == 1
    assert latin_engine_created_count() == 1
