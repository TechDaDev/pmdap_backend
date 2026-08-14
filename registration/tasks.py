"""Celery tasks for pre-registration identity extraction (ocr queue)."""
import logging
import time

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from identities.tasks import _run_ocr_and_extract
from processing.ocr import OCREngineUnavailableError
from processing.ocr_provider import engine_created_count, latin_engine_created_count
from registration.models import RegistrationIdentityExtractionJob
from registration.services import (
    _delete_staging_keys,
    clear_registration_result,
    store_registration_result,
)

logger = logging.getLogger(__name__)


def _finish(job, *, status, error_code="", keep_staging=False):
    job.status = status
    job.error_code = error_code
    update_fields = ["status", "error_code", "updated_at"]
    if not keep_staging:
        job.front_key = ""
        job.back_key = ""
        update_fields += ["front_key", "back_key"]
    job.save(update_fields=update_fields)


def _cleanup(job):
    _delete_staging_keys([job.front_key, job.back_key])
    clear_registration_result(job.uuid)


def _expire_job(job):
    _cleanup(job)
    RegistrationIdentityExtractionJob.objects.filter(pk=job.pk).delete()


@shared_task(
    bind=True,
    name="registration.process_registration_identity_extraction",
    queue="ocr",
    soft_time_limit=settings.OCR_TASK_SOFT_TIME_LIMIT,
    time_limit=settings.OCR_TASK_TIME_LIMIT,
)
def process_registration_identity_extraction(self, job_uuid):
    # Claim the job inside a short transaction; OCR runs outside it.
    with transaction.atomic():
        try:
            job = RegistrationIdentityExtractionJob.objects.select_for_update().get(
                pk=job_uuid
            )
        except RegistrationIdentityExtractionJob.DoesNotExist:
            return None
        if job.status != RegistrationIdentityExtractionJob.Status.PENDING:
            return None
        job.status = RegistrationIdentityExtractionJob.Status.PROCESSING
        job.save(update_fields=["status", "updated_at"])

    total_started = time.monotonic()
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
        _cleanup(job)
        _finish(
            job,
            status=RegistrationIdentityExtractionJob.Status.FAILED,
            error_code="OCR_UNAVAILABLE",
        )
        return None
    except Exception:
        logger.warning(
            "registration identity extraction job %s OCR failed",
            job_uuid,
            exc_info=True,
        )
        _cleanup(job)
        _finish(
            job,
            status=RegistrationIdentityExtractionJob.Status.FAILED,
            error_code="EXTRACTION_FAILED",
        )
        return None

    store_registration_result(job.uuid, payload)
    # SUCCESS keeps the staged images: the final register promotes them
    # (single upload). Result lives in the cache for the review window.
    _finish(
        job,
        status=RegistrationIdentityExtractionJob.Status.SUCCESS,
        keep_staging=True,
    )
    cleanup_registration_identity_jobs.apply_async(
        args=[str(job.uuid)],
        countdown=settings.REGISTRATION_IDENTITY_TTL_SECONDS,
        expires=settings.REGISTRATION_IDENTITY_TTL_SECONDS * 4,
    )
    # NEVER return the payload: Celery logs task return values verbatim.
    return {
        "status": "SUCCESS",
        "document_type": job.document_type,
        "mrz": payload["mrz"].get("detected"),
    }


@shared_task(name="registration.cleanup_registration_identity_jobs")
def cleanup_registration_identity_jobs(job_uuid=None):
    """Expire/cleanup a registration identity session (or sweep abandoned).

    Removes staging images, the cached extraction result and the job row. A
    FINALIZED job is already consumed by final registration and left alone.
    """
    if job_uuid is not None:
        try:
            job = RegistrationIdentityExtractionJob.objects.get(pk=job_uuid)
        except RegistrationIdentityExtractionJob.DoesNotExist:
            return None
        if job.status == RegistrationIdentityExtractionJob.Status.FINALIZED:
            return None
        # Not expired yet (race with a finalize or a still-valid session).
        if (
            job.status
            in (
                RegistrationIdentityExtractionJob.Status.PENDING,
                RegistrationIdentityExtractionJob.Status.PROCESSING,
                RegistrationIdentityExtractionJob.Status.SUCCESS,
            )
            and job.expires_at
            and job.expires_at > timezone.now()
        ):
            return None
        _expire_job(job)
        return None

    now = timezone.now()
    for job in RegistrationIdentityExtractionJob.objects.filter(
        status__in=(
            RegistrationIdentityExtractionJob.Status.PENDING,
            RegistrationIdentityExtractionJob.Status.PROCESSING,
            RegistrationIdentityExtractionJob.Status.SUCCESS,
        ),
        expires_at__lt=now,
    ):
        _expire_job(job)
    return None
