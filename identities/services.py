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

from accounts.models import User
from identities.exceptions import IdentityDocumentConflict, IdentityTransitionConflict
from identities.models import IdentityDocument, IdentityDocumentEvent, IdentityFile
from patients.models import PatientProfile

ALLOWED_IMAGE_FORMATS = {"JPEG": ("image/jpeg", ".jpg"), "PNG": ("image/png", ".png")}


@dataclass(frozen=True)
class ValidatedIdentityUpload:
    content: bytes
    original_name: str
    media_type: str
    extension: str
    sha256: str


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


def _record_event(document, event_type, actor):
    return IdentityDocumentEvent.objects.create(
        document=document,
        event_type=event_type,
        actor=actor,
        metadata={},
    )


def _sync_profile_identity_status(profile):
    cards = IdentityDocument.objects.filter(
        patient=profile,
        document_type=IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD,
    )
    if cards.filter(
        status=IdentityDocument.LifecycleStatus.CURRENT,
        verification_status=IdentityDocument.VerificationStatus.VERIFIED,
    ).exists():
        new_status = PatientProfile.IdentityStatus.VERIFIED
    elif cards.filter(
        status=IdentityDocument.LifecycleStatus.CURRENT,
        verification_status=IdentityDocument.VerificationStatus.PENDING,
    ).exists():
        new_status = PatientProfile.IdentityStatus.PENDING_VERIFICATION
    elif cards.filter(
        verification_status=IdentityDocument.VerificationStatus.REJECTED
    ).exists():
        new_status = PatientProfile.IdentityStatus.REJECTED
    else:
        new_status = PatientProfile.IdentityStatus.UNVERIFIED
    if profile.identity_status != new_status:
        PatientProfile.objects.filter(pk=profile.pk).update(identity_status=new_status)
        profile.identity_status = new_status
    return new_status


def submit_identity_document(*, patient, actor, validated_data, replaces=None):
    front_upload = validated_data.pop("front_image")
    back_upload = validated_data.pop("back_image", None)
    front_validated = inspect_identity_upload(front_upload)
    back_validated = inspect_identity_upload(back_upload) if back_upload else None
    stored_names = []
    try:
        with transaction.atomic():
            patient = PatientProfile.objects.select_for_update().get(pk=patient.pk)
            document_type = validated_data["document_type"]
            source = None
            if replaces is None:
                conflict = IdentityDocument.objects.filter(
                    patient=patient,
                    document_type=document_type,
                    status=IdentityDocument.LifecycleStatus.CURRENT,
                    verification_status__in=(
                        IdentityDocument.VerificationStatus.PENDING,
                        IdentityDocument.VerificationStatus.VERIFIED,
                    ),
                ).exists()
                if conflict:
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
                pending_exists = (
                    IdentityDocument.objects.filter(
                        patient=patient,
                        document_type=document_type,
                        status=IdentityDocument.LifecycleStatus.CURRENT,
                        verification_status=IdentityDocument.VerificationStatus.PENDING,
                    )
                    .exclude(pk=source.pk)
                    .exists()
                )
                if pending_exists:
                    raise IdentityDocumentConflict()

            front = _persist_file(front_validated)
            stored_names.append((front.file.storage, front.file.name))
            back = None
            if back_validated:
                back = _persist_file(back_validated)
                stored_names.append((back.file.storage, back.file.name))
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
            _sync_profile_identity_status(patient)
            return document
    except Exception:
        for storage, name in stored_names:
            storage.delete(name)
        raise


def approve_identity_document(*, document, agent):
    if agent.role != User.Role.IDENTITY_VERIFICATION_AGENT:
        from identities.exceptions import VerificationAgentRequired

        raise VerificationAgentRequired()
    with transaction.atomic():
        document = (
            IdentityDocument.objects.select_for_update()
            .select_related("patient", "replaces")
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
        _sync_profile_identity_status(profile)
        return document


def reject_identity_document(*, document, agent, reason):
    if agent.role != User.Role.IDENTITY_VERIFICATION_AGENT:
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
        _sync_profile_identity_status(profile)
        return document
