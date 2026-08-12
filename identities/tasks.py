"""Async identity extraction worker task (runs on the `ocr` celery queue).

Runs inside the OCR worker image where PaddleOCR + preloaded models live.
Staging images are read from private storage and deleted after processing.
Extracted values are stored in the cache (TTL); no IdentityDocument is created.
"""
import logging

from celery import shared_task
from django.conf import settings
from django.db import transaction
from PIL import Image

from identities import extraction
from identities.extraction_store import store_extraction_result
from identities.models import IdentityExtractionJob
from identities.storage import private_identity_storage
from processing.ocr import OCREngineUnavailableError, PaddleOCREngine

logger = logging.getLogger(__name__)


def _staging_keys(job):
    keys = [job.front_key]
    if job.back_key:
        keys.append(job.back_key)
    return [k for k in keys if k]


def _cleanup_staging(keys):
    for key in keys:
        if not key:
            continue
        try:
            if private_identity_storage.exists(key):
                private_identity_storage.delete(key)
        except Exception:  # pragma: no cover - storage failure path
            logger.warning(
                "identity extraction staging cleanup failed for %s",
                key,
                exc_info=True,
            )


def _finish(job, *, status, error_code="", payload=None):
    job.status = status
    job.error_code = error_code
    job.front_key = ""
    job.back_key = ""
    job.save(
        update_fields=[
            "status",
            "error_code",
            "front_key",
            "back_key",
            "updated_at",
        ]
    )
    if payload is not None:
        store_extraction_result(job.uuid, payload)


@shared_task(
    bind=True,
    name="identities.extract_identity_document",
    queue="ocr",
    soft_time_limit=settings.OCR_TASK_SOFT_TIME_LIMIT,
    time_limit=settings.OCR_TASK_TIME_LIMIT,
)
def extract_identity_document(self, job_uuid):
    # Claim the job inside a short transaction; OCR runs outside it so the row
    # lock / DB connection are not held for the whole inference.
    with transaction.atomic():
        try:
            job = IdentityExtractionJob.objects.select_for_update().get(
                pk=job_uuid
            )
        except IdentityExtractionJob.DoesNotExist:
            return None
        if job.status != IdentityExtractionJob.Status.PENDING:
            return None
        job.status = IdentityExtractionJob.Status.PROCESSING
        job.save(update_fields=["status", "updated_at"])

    keys = _staging_keys(job)
    lines = []
    try:
        engine = PaddleOCREngine()
        for key in keys:
            with private_identity_storage.open(key, "rb") as handle:
                image = Image.open(handle)
                image.load()
            try:
                result = engine.extract_image(image)
                lines.extend(line.text for line in result.lines)
            finally:
                image.close()
    except OCREngineUnavailableError:
        _cleanup_staging(keys)
        _finish(job, status=IdentityExtractionJob.Status.FAILED, error_code="OCR_UNAVAILABLE")
        return None
    except Exception:
        logger.warning(
            "identity extraction job %s OCR failed", job_uuid, exc_info=True
        )
        _cleanup_staging(keys)
        _finish(job, status=IdentityExtractionJob.Status.FAILED, error_code="EXTRACTION_FAILED")
        return None

    _cleanup_staging(keys)
    fields, warnings, mrz_summary = extraction.extract_identity(
        job.document_type, lines
    )
    payload = {
        "document_type": job.document_type,
        "extractor_version": extraction.EXTRACTOR_VERSION,
        "fields": fields,
        "warnings": warnings,
        "mrz": mrz_summary,
    }

    # Safe log: endpoint, type, status, field names + confidence buckets only.
    bucket_summary = {
        name: extraction.confidence_bucket(f["confidence"])
        for name, f in fields.items()
    }
    logger.info(
        "identity extraction job=%s type=%s ok fields=%s mrz=%s",
        job_uuid,
        job.document_type,
        bucket_summary,
        mrz_summary.get("detected"),
    )
    _finish(
        job,
        status=IdentityExtractionJob.Status.SUCCESS,
        payload=payload,
    )
    return payload
