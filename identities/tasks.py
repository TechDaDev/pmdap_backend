"""Async identity extraction worker task (runs on the `ocr` celery queue).

Runs inside the OCR worker image where PaddleOCR + preloaded models live.
Staging images are read from private storage and deleted after processing.
Extracted values are stored in the cache (TTL); no IdentityDocument is created.
"""
import io
import logging
import time
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from PIL import Image

from identities import extraction, mrz
from identities.extraction_store import (
    clear_extraction_result,
    store_extraction_result,
)
from identities.models import IdentityExtractionJob
from identities.regions import REGIONS as IRAQI_REGIONS, IraqiNationalCardRegionExtractor
from identities.storage import private_identity_storage
from processing.ocr import OCREngineUnavailableError
from processing.ocr_provider import (
    engine_created_count,
    get_latin_ocr_engine,
    get_ocr_engine,
    latin_engine_created_count,
)

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


def _finish(job, *, status, error_code="", payload=None, keep_staging=False):
    """Persist a terminal job state.

    On SUCCESS the staging keys are RETAINED (the job is finalized later by the
    document-create endpoint using the staged images). On FAILED/EXPIRED the
    staging keys are blanked after the worker already removed the objects.
    """
    job.status = status
    job.error_code = error_code
    update_fields = ["status", "error_code", "updated_at"]
    if not keep_staging:
        job.front_key = ""
        job.back_key = ""
        update_fields += ["front_key", "back_key"]
    job.save(update_fields=update_fields)
    if payload is not None:
        store_extraction_result(job.uuid, payload)


@shared_task(
    name="identities.cleanup_identity_extraction_jobs",
    queue="ocr",
    ignore_result=True,
)
def cleanup_identity_extraction_jobs(job_uuid=None):
    """Explicit expiry cleanup for identity extraction jobs.

    Two modes:
      * job_uuid=<uuid>  — cleanup one job (scheduled by the worker on SUCCESS
        with countdown = staging TTL). Never touches a FINALIZED job.
      * job_uuid=None    — sweep ALL expired/abandoned jobs (management
        command / celery beat).

    Removes private staging images, the cached result and the job row. A
    non-finalized SUCCESS job older than the staging TTL is expired.
    """
    if job_uuid is not None:
        try:
            with transaction.atomic():
                job = IdentityExtractionJob.objects.select_for_update().get(
                    pk=job_uuid
                )
                if job.status == IdentityExtractionJob.Status.FINALIZED:
                    return None
                if (
                    job.status == IdentityExtractionJob.Status.SUCCESS
                    and job.updated_at
                    and (timezone.now() - job.updated_at).total_seconds()
                    < settings.IDENTITY_STAGING_TTL_SECONDS
                ):
                    # Not expired yet (race with an earlier finalize attempt).
                    return None
                _expire_job(job)
        except IdentityExtractionJob.DoesNotExist:
            return None
        return None

    # Sweep mode: expire any abandoned job older than the staging TTL.
    deadline = timezone.now() - timedelta(
        seconds=settings.IDENTITY_STAGING_TTL_SECONDS
    )
    for job in IdentityExtractionJob.objects.filter(
        status__in=(
            IdentityExtractionJob.Status.PENDING,
            IdentityExtractionJob.Status.PROCESSING,
            IdentityExtractionJob.Status.SUCCESS,
        ),
        updated_at__lt=deadline,
    ):
        _expire_job(job)
    return None


def _expire_job(job):
    _cleanup_staging(_staging_keys(job))
    clear_extraction_result(job.uuid)
    job.status = IdentityExtractionJob.Status.EXPIRED
    job.front_key = ""
    job.back_key = ""
    job.save(
        update_fields=["status", "front_key", "back_key", "updated_at"]
    )
    job.delete()


def _adaptive_roi_plan(mrz_probe) -> dict[str, bool]:
    """Which ROI passes to SKIP given a full-OCR MRZ probe (no inference).

    A clean MRZ read from the full back-card Arabic OCR makes the ROI_MRZ
    crop redundant (the parser already consumes the back full-OCR lines), and
    a validated MRZ DOB makes the ROI_DOB crop redundant. Returns
    {ROI tag: skip?}. Always returns False for ROIs that must run.
    """
    skip_mrz = bool(mrz_probe.detected and mrz_probe.checks_passed)
    skip_dob = bool(mrz_probe.date_of_birth) and (
        "date_of_birth" not in mrz_probe.low_confidence_fields
    )
    return {
        "ROI_MRZ": skip_mrz,
        "ROI_DOB": skip_dob,
        "ROI_BLOOD": False,
        "ROI_DATES": False,
        "ROI_FAMILY": False,
    }


def _run_ocr_and_extract(*, front_key, back_key, document_type, total_started):
    """Shared OCR + deterministic extraction for identity documents.

    Reads staged private images, runs the full-card Arabic pass plus targeted
    ROI passes (Iraqi National Card), then the deterministic extractor.

    Returns (payload, timing, line_count). Raises OCREngineUnavailableError
    when the OCR runtime is unavailable; other exceptions propagate so the
    caller decides job-failure handling. Extracted values are returned only in
    memory — never logged here.
    """
    side_lines = []
    images = {}
    timing = {}
    sides = [("front", front_key)] if front_key else []
    if back_key:
        sides.append(("back", back_key))
    try:
        engine_t = time.monotonic()
        engine = get_ocr_engine()
        timing["engine_init_ms"] = int((time.monotonic() - engine_t) * 1000)
        back_full_texts: list[str] = []
        for side, key in sides:
            t = time.monotonic()
            with private_identity_storage.open(key, "rb") as handle:
                content = handle.read()
            timing[f"{side}_storage_read_ms"] = int(
                (time.monotonic() - t) * 1000
            )
            t = time.monotonic()
            image = Image.open(io.BytesIO(content))
            image.load()
            timing[f"{side}_decode_ms"] = int((time.monotonic() - t) * 1000)
            images[side] = image
            t = time.monotonic()
            result = engine.extract_image(image)
            timing[f"{side}_ocr_ms"] = int((time.monotonic() - t) * 1000)
            tag = "FRONT" if side == "front" else "BACK"
            for line in result.lines:
                if side == "back" and line.text:
                    back_full_texts.append(line.text)
                side_lines.append(
                    extraction.SideLine(tag, line.text, line.confidence)
                )

        # ADAPTIVE targeted ROI passes for the Iraqi National Card.
        #
        # The full-card Arabic pass already feeds the MRZ parser directly
        # (back lines), so a clean MRZ read from full OCR makes the ROI_MRZ
        # crop redundant. ROI_DOB is likewise redundant when the MRZ already
        # supplies a validated DOB. Skipping those two crops cuts a large
        # share of the per-job OCR cost on well-scanned cards; poor scans
        # still fall back to the ROI pass automatically (fields missing).
        #
        # ROI_BLOOD / ROI_DATES / ROI_FAMILY always run: the extractor relies
        # on these targeted crops for blood group, printed dates and the
        # family number, which the full-card Arabic pass does not reliably
        # produce.
        if document_type == "UNIFIED_NATIONAL_CARD" and images.get("back"):
            latin_t = time.monotonic()
            latin_engine = get_latin_ocr_engine()
            timing["latin_engine_init_ms"] = int(
                (time.monotonic() - latin_t) * 1000
            )
            mrz_probe = mrz.parse_iraqi_national_card_mrz(back_full_texts)
            skip_plan = _adaptive_roi_plan(mrz_probe)
            region_extractor = IraqiNationalCardRegionExtractor(
                arabic_engine=engine, latin_engine=latin_engine
            )
            for tag, spec in IRAQI_REGIONS.items():
                if skip_plan.get(tag):
                    timing[f"{tag.lower()}_ms"] = 0
                    timing[f"{tag.lower()}_skipped"] = True
                    continue
                region_image = images[spec["side"].lower()]
                t = time.monotonic()
                side_lines.extend(
                    region_extractor.extract_region(region_image, tag, spec)
                )
                timing[f"{tag.lower()}_ms"] = int((time.monotonic() - t) * 1000)
    finally:
        for image in images.values():
            try:
                image.close()
            except Exception:  # pragma: no cover - defensive close
                pass

    t = time.monotonic()
    fields, warnings, mrz_summary = extraction.extract_identity(
        document_type, side_lines
    )
    timing["parse_ms"] = int((time.monotonic() - t) * 1000)
    timing["total_ms"] = int((time.monotonic() - total_started) * 1000)
    payload = {
        "document_type": document_type,
        "extractor_version": extraction.EXTRACTOR_VERSION,
        "fields": fields,
        "warnings": warnings,
        "mrz": mrz_summary,
    }
    return payload, timing, len(side_lines)


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
    total_started = time.monotonic()
    queue_wait_ms = max(
        0, int((timezone.now() - job.created_at).total_seconds() * 1000)
    )
    created_before = engine_created_count()
    latin_created_before = latin_engine_created_count()
    try:
        payload, timing, line_count = _run_ocr_and_extract(
            front_key=job.front_key,
            back_key=job.back_key,
            document_type=job.document_type,
            total_started=total_started,
        )
    except OCREngineUnavailableError:
        _cleanup_staging(keys)
        _finish(
            job,
            status=IdentityExtractionJob.Status.FAILED,
            error_code="OCR_UNAVAILABLE",
        )
        return None
    except Exception:
        logger.warning(
            "identity extraction job %s OCR failed", job_uuid, exc_info=True
        )
        _cleanup_staging(keys)
        _finish(
            job,
            status=IdentityExtractionJob.Status.FAILED,
            error_code="EXTRACTION_FAILED",
        )
        return None

    fields = payload["fields"]
    mrz_summary = payload["mrz"]
    parse_ms = timing.get("parse_ms", 0)
    total_ms = timing.get("total_ms", 0)

    # Safe log: job, type, status, timings, line count, field names +
    # confidence buckets only. NEVER text, identity values or storage keys.
    bucket_summary = {
        name: extraction.confidence_bucket(f["confidence"])
        for name, f in fields.items()
    }
    logger.info(
        "identity extraction job=%s type=%s status=ok "
        "queue_wait_ms=%s engine_init_ms=%s engine_reused=%s "
        "latin_engine_init_ms=%s latin_engine_reused=%s "
        "front_storage_read_ms=%s front_decode_ms=%s front_ocr_ms=%s "
        "back_storage_read_ms=%s back_decode_ms=%s back_ocr_ms=%s "
        "roi_blood_ms=%s roi_dates_ms=%s roi_dob_ms=%s "
        "roi_family_ms=%s roi_mrz_ms=%s "
        "roi_mrz_skipped=%s roi_dob_skipped=%s "
        "parse_ms=%s total_ms=%s line_count=%s fields=%s mrz=%s",
        job_uuid,
        job.document_type,
        queue_wait_ms,
        timing.get("engine_init_ms"),
        created_before > 0,
        timing.get("latin_engine_init_ms"),
        latin_created_before > 0,
        timing.get("front_storage_read_ms"),
        timing.get("front_decode_ms"),
        timing.get("front_ocr_ms"),
        timing.get("back_storage_read_ms"),
        timing.get("back_decode_ms"),
        timing.get("back_ocr_ms"),
        timing.get("roi_blood_ms"),
        timing.get("roi_dates_ms"),
        timing.get("roi_dob_ms"),
        timing.get("roi_family_ms"),
        timing.get("roi_mrz_ms"),
        bool(timing.get("roi_mrz_skipped")),
        bool(timing.get("roi_dob_skipped")),
        parse_ms,
        total_ms,
        line_count,
        bucket_summary,
        mrz_summary.get("detected"),
    )
    # SUCCESS keeps the staging images: the client finalizes later through the
    # document-create endpoint using extraction_job_id (single upload).
    _finish(
        job,
        status=IdentityExtractionJob.Status.SUCCESS,
        payload=payload,
        keep_staging=True,
    )
    # Schedule expiry cleanup so abandoned staging never persists forever.
    cleanup_identity_extraction_jobs.apply_async(
        args=[str(job.uuid)],
        countdown=settings.IDENTITY_STAGING_TTL_SECONDS,
        expires=settings.IDENTITY_STAGING_TTL_SECONDS * 4,
    )
    # NEVER return the payload: Celery logs the task return value verbatim,
    # which would leak extracted identity values into worker logs. Return a
    # safe summary (field names + confidence buckets only).
    return {
        "status": "SUCCESS",
        "document_type": job.document_type,
        "fields": bucket_summary,
        "mrz": mrz_summary.get("detected"),
    }
