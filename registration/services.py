"""Service layer for scan-first registration identity sessions.

Capability-bound: the client holds job_id + job_token; only a SHA-256 digest
is stored. Images are uploaded exactly once and promoted to permanent storage
during the single atomic final registration transaction. Nothing here logs or
persists extracted identity values.
"""
import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers

from audit.models import AuditLog
from audit.services import record_audit
from identities.exceptions import (
    IdentityExtractionJobMismatch,
    IdentityFileStorageFailed,
)
from identities.services import _create_document, _read_staged_validated
from identities.storage import private_identity_storage
from patients.models import PatientProfile
from patients.services import create_patient_profile
from registration.exceptions import (
    RegistrationIdentityJobConflict,
    RegistrationIdentityJobExpired,
    RegistrationIdentityJobNotFound,
    RegistrationIdentityStorageFailed,
)
from registration.models import RegistrationIdentityExtractionJob

CACHE_PREFIX = "registration:extract:"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _constant_time_equal(left: str, right: str) -> bool:
    return secrets.compare_digest(left, right)


def _staging_key(job, side: str, upload) -> str:
    lower = (getattr(upload, "name", "") or "").lower()
    extension = ".png" if lower.endswith(".png") else ".jpg"
    return f"registration_staging/{job.uuid}/{side}{extension}"


def _store_upload(job, side, upload) -> str:
    key = _staging_key(job, side, upload)
    try:
        upload.seek(0)
        private_identity_storage.save(key, upload)
    except Exception as exc:
        raise RegistrationIdentityStorageFailed() from exc
    return key


def _delete_staging_keys(keys):
    for key in keys:
        if not key:
            continue
        try:
            if private_identity_storage.exists(key):
                private_identity_storage.delete(key)
        except Exception:  # pragma: no cover - storage failure path
            pass


def store_registration_result(job_uuid, payload):
    cache.set(
        f"{CACHE_PREFIX}{job_uuid}",
        payload,
        settings.REGISTRATION_IDENTITY_CACHE_TTL_SECONDS,
    )


def read_registration_result(job_uuid):
    return cache.get(f"{CACHE_PREFIX}{job_uuid}")


def clear_registration_result(job_uuid):
    cache.delete(f"{CACHE_PREFIX}{job_uuid}")


def issue_registration_job(*, document_type, front_upload, back_upload):
    """Create a capability-bound registration extraction session.

    Images are staged once. Returns (job, job_token); the token is returned to
    the client exactly once, only its digest is stored.
    """
    token = secrets.token_urlsafe(48)
    job = RegistrationIdentityExtractionJob.objects.create(
        capability_digest=_digest(token),
        document_type=document_type,
        expires_at=timezone.now()
        + timedelta(seconds=settings.REGISTRATION_IDENTITY_TTL_SECONDS),
    )
    front_key = ""
    back_key = ""
    try:
        front_key = _store_upload(job, "front", front_upload)
        back_key = _store_upload(job, "back", back_upload)
        job.front_key = front_key
        job.back_key = back_key
        job.save(update_fields=["front_key", "back_key", "updated_at"])
    except Exception:
        _delete_staging_keys([front_key, back_key])
        RegistrationIdentityExtractionJob.objects.filter(pk=job.pk).delete()
        raise
    return job, token


def _expire_job(job):
    """Cleanup an expired/abandoned registration session."""
    _delete_staging_keys([job.front_key, job.back_key])
    clear_registration_result(job.uuid)
    RegistrationIdentityExtractionJob.objects.filter(pk=job.pk).delete()


def get_job_for_poll(*, job_id, token):
    """Capability-checked job lookup for the public poll endpoint.

    A wrong/missing token returns 404 (no existence leak). Expired/FINALIZED
    sessions map to safe domain errors.
    """
    if not token:
        raise RegistrationIdentityJobNotFound()
    try:
        job = RegistrationIdentityExtractionJob.objects.get(uuid=job_id)
    except RegistrationIdentityExtractionJob.DoesNotExist:
        raise RegistrationIdentityJobNotFound() from None
    if not _constant_time_equal(_digest(token), job.capability_digest):
        raise RegistrationIdentityJobNotFound()
    if job.status == RegistrationIdentityExtractionJob.Status.FINALIZED:
        raise RegistrationIdentityJobConflict()
    if job.status == RegistrationIdentityExtractionJob.Status.EXPIRED or (
        job.expires_at and job.expires_at <= timezone.now()
    ):
        _expire_job(job)
        raise RegistrationIdentityJobExpired()
    return job


def _check_identity_conflicts(identity):
    """Safe duplicate checks for the Iraqi card identifiers.

    The visible card number (national_card_number, carried in
    document_number/national_number by the backend alias) and the physical
    body number are per-card unique. family_number is INTENTIONALLY not unique
    (family members share it) and is never checked — family-linking is Step 3.
    """
    card = (
        identity.get("national_card_number")
        or identity.get("document_number")
        or ""
    ).strip()
    body = (identity.get("unique_card_body_number") or "").strip()
    if not card and not body:
        return
    from identities.models import IdentityDocument

    q = Q()
    if card:
        q |= Q(document_number=card) | Q(national_number=card)
    if body:
        q |= Q(unique_card_body_number=body)
    if IdentityDocument.objects.filter(q).exists():
        raise serializers.ValidationError(
            {"registration_identity": ["This National Card is already registered."]}
        )


@transaction.atomic
def finalize_scan_first_registration(
    *, email, password, phone, governorate, registration_identity
):
    """Atomically create User + PatientProfile + pending IdentityDocument.

    The capability is validated inside the transaction (select_for_update).
    On any failure the account and document are rolled back and the session is
    NOT consumed, so the client can correct and retry while the TTL is valid.
    """
    user_model = get_user_model()
    job_id = registration_identity["job_id"]
    token = registration_identity["job_token"]

    job = (
        RegistrationIdentityExtractionJob.objects.select_for_update()
        .filter(uuid=job_id)
        .first()
    )
    if job is None:
        raise RegistrationIdentityJobNotFound()
    if not _constant_time_equal(_digest(token), job.capability_digest):
        raise RegistrationIdentityJobNotFound()
    if job.status == RegistrationIdentityExtractionJob.Status.FINALIZED:
        raise RegistrationIdentityJobConflict()
    if job.status == RegistrationIdentityExtractionJob.Status.EXPIRED or (
        job.expires_at and job.expires_at <= timezone.now()
    ):
        _expire_job(job)
        raise RegistrationIdentityJobExpired()
    if job.status != RegistrationIdentityExtractionJob.Status.SUCCESS:
        raise RegistrationIdentityJobConflict()
    if job.document_type != registration_identity["document_type"]:
        raise RegistrationIdentityJobConflict()

    # Per-card duplicate protection (family_number excluded by design).
    _check_identity_conflicts(registration_identity)

    try:
        user = user_model.objects.create_user(
            email=email,
            password=password,
            phone=phone,
            role=user_model.Role.PATIENT,
            status=user_model.Status.ACTIVE,
            is_active=True,
            is_staff=False,
            is_superuser=False,
            email_verified=False,
            phone_verified=False,
        )
    except IntegrityError as exc:
        # Job is NOT consumed on duplicate email: user can correct and retry.
        raise serializers.ValidationError(
            {"email": ["An account with this email already exists."]}
        ) from exc

    # Structured patronymic components are canonical; full_name is the
    # deterministic join for existing app/UI expectations.
    name = registration_identity["name"]
    father_name = registration_identity["father_name"]
    grandfather_name = registration_identity["grandfather_name"]
    full_name = " ".join(p for p in (name, father_name, grandfather_name) if p)

    profile = create_patient_profile(
        user=user,
        full_name=full_name,
        given_name=name,
        father_name=father_name,
        grandfather_name=grandfather_name,
        governorate=governorate,
        date_of_birth=registration_identity["date_of_birth"],
        sex=registration_identity["sex"],
        nationality=registration_identity["nationality"],
        blood_group=registration_identity.get(
            "blood_group", PatientProfile.BloodGroup.UNKNOWN
        ),
    )
    record_audit(
        action=AuditLog.Action.PATIENT_PROFILE_CREATED,
        actor=user,
        patient=profile,
        resource_type="PATIENT_PROFILE",
        resource_uuid=profile.uuid,
        new_values={
            "identity_status": profile.identity_status,
            "digital_id": profile.digital_id,
        },
    )

    # Promote the staged images (single upload) into permanent files + a
    # PENDING identity document. `_create_document` rolls back its promoted
    # objects on failure.
    try:
        front_validated = _read_staged_validated(job, job.front_key, "front")
        back_validated = (
            _read_staged_validated(job, job.back_key, "back")
            if job.back_key
            else None
        )
    except IdentityExtractionJobMismatch as exc:
        raise RegistrationIdentityJobConflict() from exc
    except IdentityFileStorageFailed as exc:
        raise RegistrationIdentityStorageFailed() from exc

    _create_document(
        patient=profile,
        actor=user,
        validated_data={
            "document_type": job.document_type,
            "document_number": registration_identity["document_number"],
            "national_number": registration_identity.get(
                "national_card_number", ""
            ),
            "family_number": registration_identity.get("family_number", ""),
            "unique_card_body_number": registration_identity.get(
                "unique_card_body_number", ""
            ),
            "issuing_country": "IQ",
        },
        front_validated=front_validated,
        back_validated=back_validated,
    )

    # Single-use consumption, committed atomically with the account+document.
    job.status = RegistrationIdentityExtractionJob.Status.FINALIZED
    job.save(update_fields=["status", "updated_at"])

    # Post-commit: remove staging + cached result + consumed row. On rollback
    # this never runs, so staging is preserved for a safe retry.
    transaction.on_commit(
        lambda: cleanup_registration_job_after_finalize(job)
    )

    record_audit(
        action=AuditLog.Action.ACCOUNT_CREATED,
        actor=user,
        patient=profile,
        resource_type="USER",
        resource_uuid=user.uuid,
        new_values={"role": user.role, "status": user.status},
    )
    return user


def cleanup_registration_job_after_finalize(job):
    """Remove staging + cached result after a committed finalization."""
    _delete_staging_keys([job.front_key, job.back_key])
    clear_registration_result(job.uuid)
    RegistrationIdentityExtractionJob.objects.filter(pk=job.pk).delete()
