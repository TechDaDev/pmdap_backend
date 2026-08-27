import hashlib
import io
import uuid
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone
from PIL import Image, UnidentifiedImageError

from audit.models import AuditLog
from audit.services import record_audit
from identities.exceptions import (
    IdentityDocumentConflict,
    IdentityExtractionJobMismatch,
    IdentityFileStorageFailed,
    IdentityTransitionConflict,
)
from identities.models import (
    IdentityDocument,
    IdentityDocumentEvent,
    IdentityExtractionJob,
    IdentityFile,
)
from identities.permissions import can_verify_identity
from identities.storage import private_identity_storage
from patients.models import PatientProfile

try:
    from botocore.exceptions import ClientError as _S3ClientError
except ImportError:  # pragma: no cover - S3 client is optional in minimal installs
    _S3ClientError = OSError

ALLOWED_IMAGE_FORMATS = {"JPEG": ("image/jpeg", ".jpg"), "PNG": ("image/png", ".png")}


@dataclass(frozen=True)
class ValidatedIdentityUpload:
    content: bytes
    original_name: str
    media_type: str
    extension: str
    sha256: str


def _media_type_for_key(key: str) -> str:
    return "image/png" if (key or "").lower().endswith(".png") else "image/jpeg"


class _BytesUpload:
    """Minimal UploadedFile-like wrapper over raw bytes.

    Lets [inspect_identity_upload] re-validate staged identity images before
    they are promoted to permanent storage.
    """

    def __init__(self, content, name, content_type):
        self._content = bytes(content)
        self.name = name
        self.content_type = content_type
        self._pos = 0

    @property
    def size(self):
        return len(self._content)

    def read(self, size=-1):
        if size is None or size < 0:
            size = len(self._content) - self._pos
        data = self._content[self._pos : self._pos + size]
        self._pos += len(data)
        return data

    def seek(self, offset, whence=0):
        if whence == 0:
            self._pos = offset
        elif whence == 1:
            self._pos += offset
        elif whence == 2:
            self._pos = len(self._content) + offset
        else:  # pragma: no cover - only used internally
            raise ValueError("unsupported whence")


def inspect_identity_upload(upload):
    limit = settings.IDENTITY_FILE_MAX_BYTES
    if upload.size == 0:
        raise ValidationError("Identity image cannot be empty.")
    if upload.size > limit:
        raise ValidationError("Identity image exceeds the configured size limit.")
    upload.seek(0)
    content = upload.read(limit + 1)
    upload.seek(0)
    if not content:
        raise ValidationError("Identity image cannot be empty.")
    if len(content) > limit:
        raise ValidationError("Identity image exceeds the configured size limit.")
    if upload.content_type not in {"image/jpeg", "image/png"}:
        raise ValidationError("Identity image must be JPEG or PNG.")
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.verify()
            image_format = image.format
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ValidationError("Identity image content is malformed.") from exc
    expected = ALLOWED_IMAGE_FORMATS.get(image_format)
    if expected is None or upload.content_type != expected[0]:
        raise ValidationError("Declared MIME type does not match image content.")
    has_exact_terminator = (
        image_format == "PNG" and content.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82")
    ) or (image_format == "JPEG" and content.endswith(b"\xff\xd9"))
    if not has_exact_terminator:
        raise ValidationError("Identity image contains trailing or invalid content.")
    return ValidatedIdentityUpload(
        content=content,
        original_name=Path(upload.name).name[:255],
        media_type=expected[0],
        extension=expected[1],
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _persist_file(validated):
    identity_file = IdentityFile(
        original_name=validated.original_name,
        media_type=validated.media_type,
        size=len(validated.content),
        sha256=validated.sha256,
    )
    name = f"{uuid.uuid4().hex}{validated.extension}"
    identity_file.file.save(name, ContentFile(validated.content), save=False)
    try:
        identity_file.save()
    except Exception:
        identity_file.file.storage.delete(identity_file.file.name)
        raise
    return identity_file


def persist_identity_upload(upload):
    return _persist_file(inspect_identity_upload(upload))


def delete_identity_file_from_storage(identity_file):
    if identity_file and identity_file.file.name:
        identity_file.file.storage.delete(identity_file.file.name)


def _record_event(document, event_type, actor):
    return IdentityDocumentEvent.objects.create(
        document=document,
        event_type=event_type,
        actor=actor,
        metadata={},
    )


def _sync_profile_identity_status(profile):
    primary_types = [IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD]
    if profile.is_minor:
        primary_types.append(IdentityDocument.DocumentType.BIRTH_DOCUMENT)
    documents = IdentityDocument.objects.filter(
        patient=profile, document_type__in=primary_types
    )
    if documents.filter(
        status=IdentityDocument.LifecycleStatus.CURRENT,
        verification_status=IdentityDocument.VerificationStatus.VERIFIED,
    ).exists():
        new_status = PatientProfile.IdentityStatus.VERIFIED
    elif documents.filter(
        status=IdentityDocument.LifecycleStatus.CURRENT,
        verification_status=IdentityDocument.VerificationStatus.PENDING,
    ).exists():
        new_status = PatientProfile.IdentityStatus.PENDING_VERIFICATION
    elif documents.filter(
        verification_status=IdentityDocument.VerificationStatus.REJECTED
    ).exists():
        new_status = PatientProfile.IdentityStatus.REJECTED
    else:
        new_status = PatientProfile.IdentityStatus.UNVERIFIED
    if profile.identity_status != new_status:
        previous = profile.identity_status
        PatientProfile.objects.filter(pk=profile.pk).update(identity_status=new_status)
        profile.identity_status = new_status
        record_audit(
            action=AuditLog.Action.PATIENT_IDENTITY_STATUS_CHANGED,
            actor_type=AuditLog.ActorType.SYSTEM,
            patient=profile,
            resource_type="PATIENT",
            resource_uuid=profile.uuid,
            previous_values={"identity_status": previous},
            new_values={"identity_status": new_status},
        )
    return new_status


def _current_type_conflict(patient, document_type, exclude_pk=None):
    """True when a CURRENT PENDING/VERIFIED document of the same type exists."""
    queryset = IdentityDocument.objects.filter(
        patient=patient,
        document_type=document_type,
        status=IdentityDocument.LifecycleStatus.CURRENT,
        verification_status__in=(
            IdentityDocument.VerificationStatus.PENDING,
            IdentityDocument.VerificationStatus.VERIFIED,
        ),
    )
    if exclude_pk is not None:
        queryset = queryset.exclude(pk=exclude_pk)
    return queryset.exists()


def _create_document(
    *,
    patient,
    actor,
    validated_data,
    front_validated,
    back_validated,
    replaces=None,
):
    """Create an IdentityDocument from validated image bytes inside a
    transaction. Shared by the legacy multipart submit and the
    extraction-job finalize paths so conflict/audit/event logic cannot drift.

    On failure any promoted permanent file objects (and their IdentityFile
    rows) are removed; nothing is left behind as an orphan.
    """
    stored = []
    try:
        with transaction.atomic():
            patient = PatientProfile.objects.select_for_update().get(pk=patient.pk)
            document_type = validated_data["document_type"]
            source = None
            if replaces is None:
                if _current_type_conflict(patient, document_type):
                    raise IdentityDocumentConflict()
            else:
                source = IdentityDocument.objects.select_for_update().get(
                    pk=replaces.pk, patient=patient
                )
                if (
                    source.status != IdentityDocument.LifecycleStatus.CURRENT
                    or source.verification_status
                    != IdentityDocument.VerificationStatus.VERIFIED
                    or source.document_type != document_type
                ):
                    raise IdentityTransitionConflict()
                if _current_type_conflict(patient, document_type, exclude_pk=source.pk):
                    raise IdentityDocumentConflict()

            front = _persist_file(front_validated)
            stored.append((front.file.storage, front.file.name, front.pk))
            back = None
            if back_validated:
                back = _persist_file(back_validated)
                stored.append((back.file.storage, back.file.name, back.pk))
            document = IdentityDocument.objects.create(
                patient=patient,
                front_image=front,
                back_image=back,
                replaces=source,
                **validated_data,
            )
            event_type = (
                IdentityDocumentEvent.EventType.REPLACEMENT_SUBMITTED
                if source
                else IdentityDocumentEvent.EventType.UPLOADED
            )
            _record_event(document, event_type, actor)
            record_audit(
                action=(
                    AuditLog.Action.IDENTITY_DOCUMENT_REPLACED
                    if source
                    else AuditLog.Action.IDENTITY_DOCUMENT_UPLOADED
                ),
                actor=actor,
                patient=patient,
                resource_type="IDENTITY_DOCUMENT",
                resource_uuid=document.uuid,
                new_values={
                    "document_type": document.document_type,
                    "status": document.status,
                    "verification_status": document.verification_status,
                },
                metadata={
                    "replaces": str(source.uuid) if source else None,
                    "identity_file": str(front.uuid),
                },
            )
            _sync_profile_identity_status(patient)
            return document
    except Exception:
        # Storage is external to the DB transaction: remove any promoted
        # objects AND their IdentityFile rows so nothing is orphaned.
        for storage, name, identity_file_pk in stored:
            try:
                storage.delete(name)
            except Exception:  # pragma: no cover - storage failure path
                pass
            IdentityFile.objects.filter(pk=identity_file_pk).delete()
        raise


def submit_identity_document(*, patient, actor, validated_data, replaces=None):
    """LEGACY direct-multipart submit (images uploaded with this request)."""
    front_upload = validated_data.pop("front_image")
    back_upload = validated_data.pop("back_image", None)
    front_validated = inspect_identity_upload(front_upload)
    back_validated = inspect_identity_upload(back_upload) if back_upload else None
    return _create_document(
        patient=patient,
        actor=actor,
        validated_data=validated_data,
        front_validated=front_validated,
        back_validated=back_validated,
        replaces=replaces,
    )


def _read_staged_validated(job, key, name):
    """Read + re-validate a staged identity image before promotion.

    Staging is not trusted just because OCR succeeded: the same identity
    upload safety rules are applied again.
    """
    if not key:
        raise IdentityExtractionJobMismatch(f"{name} image is missing.")
    try:
        with private_identity_storage.open(key, "rb") as handle:
            content = handle.read()
    except (OSError, _S3ClientError) as exc:
        raise IdentityFileStorageFailed() from exc
    if not content:
        raise IdentityExtractionJobMismatch(f"{name} image is empty.")
    upload = _BytesUpload(
        content,
        f"{name}.{_media_type_for_key(key).split('/')[1]}",
        _media_type_for_key(key),
    )
    try:
        return inspect_identity_upload(upload)
    except ValidationError as exc:
        # Never leak the validation detail; the staged content is unusable.
        raise IdentityExtractionJobMismatch(
            "Staged identity image failed validation."
        ) from exc


def finalize_identity_document(
    *,
    patient,
    actor,
    validated_data,
    job,
    replaces=None,
    defer_cleanup=False,
):
    """Finalize a successful extraction job into a real IdentityDocument.

    The job's staged images are promoted to permanent storage. The job is
    consumed exactly once (single-use), guarded by select_for_update inside
    the same transaction as document creation.

    Ownership: only the job owner (request.user == job.user) can finalize.
    A different user receives a 404 (existence is not revealed).

    Staging objects + cached result + the consumed job row are removed after
    finalization. Callers that wrap this service in a larger transaction may
    defer cleanup until that outer transaction commits, preserving staging on
    rollback.
    """
    validated_data = {
        key: value
        for key, value in validated_data.items()
        if key not in ("front_image", "back_image")
    }
    with transaction.atomic():
        job = IdentityExtractionJob.objects.select_for_update().get(pk=job.pk)
        if job.user_id != actor.pk:
            # 404 — never reveal that the job exists for another user.
            from identities.exceptions import IdentityExtractionJobNotFound

            raise IdentityExtractionJobNotFound()
        if job.status != IdentityExtractionJob.Status.SUCCESS:
            if job.status == IdentityExtractionJob.Status.EXPIRED:
                from identities.exceptions import IdentityExtractionJobExpired

                raise IdentityExtractionJobExpired()
            from identities.exceptions import IdentityExtractionJobConflict

            raise IdentityExtractionJobConflict()
        if job.document_type != validated_data["document_type"]:
            raise IdentityExtractionJobMismatch()
        if (
            validated_data["document_type"]
            == IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD
            and not job.back_key
        ):
            raise IdentityExtractionJobMismatch("Back image staging is missing.")

        front_validated = _read_staged_validated(job, job.front_key, "front")
        back_validated = (
            _read_staged_validated(job, job.back_key, "back") if job.back_key else None
        )
        document = _create_document(
            patient=patient,
            actor=actor,
            validated_data=validated_data,
            front_validated=front_validated,
            back_validated=back_validated,
            replaces=replaces,
        )
        # Single-use guard committed atomically with the document.
        job.status = IdentityExtractionJob.Status.FINALIZED
        job.save(update_fields=["status", "updated_at"])
    # Committed: remove staging + cached result + consumed job row.
    if defer_cleanup:
        transaction.on_commit(lambda: _finalize_job_cleanup(job))
    else:
        _finalize_job_cleanup(job)
    return document


def _finalize_job_cleanup(job):
    """Remove staging + cached result after a committed finalization."""
    from identities.extraction_store import clear_extraction_result

    if job:
        for key in (job.front_key, job.back_key):
            if not key:
                continue
            try:
                if private_identity_storage.exists(key):
                    private_identity_storage.delete(key)
            except Exception:  # pragma: no cover - storage failure path
                pass
        clear_extraction_result(job.uuid)
        job.delete()


def approve_identity_document(*, document, agent):
    if not can_verify_identity(agent):
        from identities.exceptions import VerificationAgentRequired

        raise VerificationAgentRequired()
    with transaction.atomic():
        document = (
            IdentityDocument.objects.select_for_update()
            .select_related("patient")
            .get(pk=document.pk)
        )
        profile = PatientProfile.objects.select_for_update().get(pk=document.patient_id)
        if document.verification_status == IdentityDocument.VerificationStatus.VERIFIED:
            if document.verified_by_id == agent.pk:
                return document
            raise IdentityTransitionConflict()
        if document.verification_status != IdentityDocument.VerificationStatus.PENDING:
            raise IdentityTransitionConflict()

        if document.replaces_id:
            previous = IdentityDocument.objects.select_for_update().get(
                pk=document.replaces_id
            )
            if (
                previous.status != IdentityDocument.LifecycleStatus.CURRENT
                or previous.verification_status
                != IdentityDocument.VerificationStatus.VERIFIED
            ):
                raise IdentityTransitionConflict()
            previous.status = IdentityDocument.LifecycleStatus.REPLACED
            previous.save(update_fields=("status", "updated_at"))
            _record_event(previous, IdentityDocumentEvent.EventType.REPLACED, agent)
            record_audit(
                action=AuditLog.Action.IDENTITY_DOCUMENT_REPLACED,
                actor=agent,
                patient=document.patient,
                resource_type="IDENTITY_DOCUMENT",
                resource_uuid=document.uuid,
                previous_values={"status": IdentityDocument.LifecycleStatus.CURRENT},
                new_values={"status": IdentityDocument.LifecycleStatus.REPLACED},
                metadata={"replaced_document": str(previous.uuid)},
            )
        elif (
            IdentityDocument.objects.filter(
                patient_id=document.patient_id,
                document_type=document.document_type,
                status=IdentityDocument.LifecycleStatus.CURRENT,
                verification_status=IdentityDocument.VerificationStatus.VERIFIED,
            )
            .exclude(pk=document.pk)
            .exists()
        ):
            raise IdentityTransitionConflict()

        # M29.5: promote staged reviewer-reviewed values to authoritative
        # stores (PatientProfile structured fields + document number columns).
        from identities.corrections import (
            _apply_reviewed_to_authoritative,
            _check_correction_conflicts,
        )

        changed_profile, changed_document = _apply_reviewed_to_authoritative(
            document, profile
        )
        if changed_document:
            _check_correction_conflicts(document, profile)
        if changed_profile:
            profile.save(
                update_fields=tuple(changed_profile)
                + ("full_name", "updated_at")
            )
        if changed_document:
            document.save(
                update_fields=tuple(changed_document) + ("updated_at",)
            )

        document.verification_status = IdentityDocument.VerificationStatus.VERIFIED
        document.verified_by = agent
        document.verified_at = timezone.now()
        document.rejection_reason = ""
        document.save(
            update_fields=(
                "verification_status",
                "verified_by",
                "verified_at",
                "rejection_reason",
                "updated_at",
            )
        )
        _record_event(document, IdentityDocumentEvent.EventType.VERIFIED, agent)
        record_audit(
            action=AuditLog.Action.IDENTITY_DOCUMENT_VERIFIED,
            actor=agent,
            patient=document.patient,
            resource_type="IDENTITY_DOCUMENT",
            resource_uuid=document.uuid,
            previous_values={
                "verification_status": IdentityDocument.VerificationStatus.PENDING
            },
            new_values={
                "verification_status": IdentityDocument.VerificationStatus.VERIFIED
            },
        )
        _sync_profile_identity_status(profile)
        if (
            document.document_type
            == IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD
        ):
            from guardians.services import revalidate_relationships_for_identity

            revalidate_relationships_for_identity(patient=profile, actor=agent)
        return document


def reject_identity_document(*, document, agent, reason):
    if not can_verify_identity(agent):
        from identities.exceptions import VerificationAgentRequired

        raise VerificationAgentRequired()
    with transaction.atomic():
        document = (
            IdentityDocument.objects.select_for_update()
            .select_related("patient")
            .get(pk=document.pk)
        )
        profile = PatientProfile.objects.select_for_update().get(pk=document.patient_id)
        if document.verification_status != IdentityDocument.VerificationStatus.PENDING:
            raise IdentityTransitionConflict()
        document.verification_status = IdentityDocument.VerificationStatus.REJECTED
        document.status = IdentityDocument.LifecycleStatus.REVOKED
        document.verified_by = agent
        document.verified_at = timezone.now()
        document.rejection_reason = reason
        document.save(
            update_fields=(
                "verification_status",
                "status",
                "verified_by",
                "verified_at",
                "rejection_reason",
                "updated_at",
            )
        )
        _record_event(document, IdentityDocumentEvent.EventType.REJECTED, agent)
        record_audit(
            action=AuditLog.Action.IDENTITY_DOCUMENT_REJECTED,
            actor=agent,
            patient=document.patient,
            resource_type="IDENTITY_DOCUMENT",
            resource_uuid=document.uuid,
            previous_values={
                "verification_status": IdentityDocument.VerificationStatus.PENDING
            },
            new_values={
                "verification_status": IdentityDocument.VerificationStatus.REJECTED
            },
        )
        _sync_profile_identity_status(profile)
        return document
