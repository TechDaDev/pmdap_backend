"""M29.5 reviewer identity correction domain services.

Three explicit authority layers (never conflated):

1. RAW OCR evidence - machine-produced; never overwritten. The authoritative
   value present before a correction is preserved as
   ``IdentityFieldCorrection.original_value``.
2. REVIEWED values - staged on ``IdentityDocument.reviewed_*``; authoritative
   stores stay untouched until approval (or an explicit verified correction).
3. VERIFIED authoritative values - ``PatientProfile`` structured fields and
   ``IdentityDocument`` number fields.

Never silently overwrite OCR evidence. Never let a correction promote to the
profile before approval. Never let a rejection promote corrections.
"""
from __future__ import annotations

import unicodedata
from datetime import date, datetime

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from audit.models import AuditLog
from audit.services import record_audit
from identities.exceptions import (
    IdentityCorrectionConflict,
    IdentityTransitionConflict,
    StaleReviewConflict,
    VerificationAgentRequired,
)
from identities.models import (
    IdentityDocument,
    IdentityDocumentEvent,
    IdentityFieldCorrection,
)
from identities.permissions import can_verify_identity
from patients.models import PatientProfile

# Whitelisted editable structured identity fields (reviewer may correct ONLY
# these). No status / role / owner / UUID / storage fields.
PROFILE_FIELDS = {
    "given_name",
    "father_name",
    "grandfather_name",
    "mother_name",
    "date_of_birth",
    "sex",
    "blood_group",
    "nationality",
}
DOCUMENT_FIELDS = {
    "document_number",
    "national_number",
    "family_number",
    "unique_card_body_number",
}
REVIEWABLE_FIELDS = PROFILE_FIELDS | DOCUMENT_FIELDS

NAME_FIELDS = {"given_name", "father_name", "grandfather_name", "mother_name"}
NUMBER_FIELDS = {
    "document_number",
    "national_number",
    "family_number",
    "unique_card_body_number",
}

_REASON_CATEGORIES = {
    c.value for c in IdentityFieldCorrection.ReasonCategory
}


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _safe_name(value: str) -> bool:
    """Unicode letters/marks + space/apostrophe/hyphen/dot only (no digits)."""
    for ch in value:
        cat = unicodedata.category(ch)
        if cat.startswith("L") or cat.startswith("M"):
            continue
        if ch in (" ", "'", "-", "."):
            continue
        return False
    return True


def _safe_number(value: str) -> bool:
    """Alphanumeric + space/hyphen/slash only (Iraqi card number formats)."""
    for ch in value:
        if ch.isalnum() or ch in ("-", "/", " "):
            continue
        return False
    return True


def normalize_review_value(field: str, value):
    """Normalize a review field value (whitespace/uppercase), no validation."""
    if field in NAME_FIELDS:
        return " ".join((value or "").split())
    if field in NUMBER_FIELDS:
        return (value or "").strip()
    if field == "nationality":
        return (value or "").strip().upper()
    return value


def validate_review_value(field: str, value):
    """Validate + normalize a single review field. Raises ValidationError.

    Names: required (except mother_name), Unicode-safe, <=255.
    date_of_birth: valid date, not future, reasonable range (minors allowed).
    sex/blood_group: existing enums only.
    nationality: ISO alpha-2.
    Numbers: safe charset, length bounds, document_number required.
    """
    from django.core.exceptions import ValidationError

    if field not in REVIEWABLE_FIELDS:
        raise ValidationError(f"{field} is not an editable identity field.")

    if field in NAME_FIELDS:
        value = normalize_review_value(field, value)
        if field != "mother_name" and not value:
            raise ValidationError(f"{field} is required.")
        if len(value) > 255:
            raise ValidationError(f"{field} is too long.")
        if not _safe_name(value):
            raise ValidationError(f"{field} contains unsupported characters.")
        return value

    if field == "date_of_birth":
        if isinstance(value, str):
            try:
                value = date.fromisoformat(value)
            except ValueError:
                raise ValidationError(
                    "date_of_birth must be a valid date."
                ) from None
        if not isinstance(value, date):
            raise ValidationError("date_of_birth must be a valid date.")
        today = timezone.localdate()
        if value > today:
            raise ValidationError("Date of birth cannot be in the future.")
        if value.year < 1900:
            raise ValidationError("Date of birth is out of a reasonable range.")
        return value

    if field == "sex":
        if value not in PatientProfile.Sex.values:
            raise ValidationError("sex is not a valid choice.")
        return value

    if field == "blood_group":
        if value not in PatientProfile.BloodGroup.values:
            raise ValidationError("blood_group is not a valid choice.")
        return value

    if field == "nationality":
        value = normalize_review_value(field, value)
        if len(value) != 2 or not value.isalpha() or not value.isupper():
            raise ValidationError("nationality must be an ISO alpha-2 code.")
        return value

    if field in NUMBER_FIELDS:
        value = normalize_review_value(field, value)
        if field == "document_number" and not value:
            raise ValidationError("document_number is required.")
        if len(value) > 128:
            raise ValidationError(f"{field} is too long.")
        if not _safe_number(value):
            raise ValidationError(f"{field} contains unsupported characters.")
        return value

    raise ValidationError(f"{field} is not an editable identity field.")


def _original_value(document, profile, field: str) -> str:
    source = profile if field in PROFILE_FIELDS else document
    return _as_text(getattr(source, field))


def _typed_value(field: str, value):
    if field == "date_of_birth":
        return value  # date object
    return _as_text(value)


def _record_event(document, event_type, actor):
    return IdentityDocumentEvent.objects.create(
        document=document, event_type=event_type, actor=actor, metadata={}
    )


def _check_correction_conflicts(document, profile):
    """Block approval/correction when corrected card identifiers collide with
    another CURRENT identity (national/card-body numbers are per-card unique;
    family_number is intentionally shared)."""
    card = (document.national_number or document.document_number or "").strip()
    body = (document.unique_card_body_number or "").strip()
    if not card and not body:
        return
    q = Q()
    if card:
        q |= Q(document_number=card) | Q(national_number=card)
    if body:
        q |= Q(unique_card_body_number=body)
    collision = (
        IdentityDocument.objects.filter(q)
        .filter(
            status=IdentityDocument.LifecycleStatus.CURRENT,
            verification_status__in=(
                IdentityDocument.VerificationStatus.PENDING,
                IdentityDocument.VerificationStatus.VERIFIED,
            ),
        )
        .exclude(pk=document.pk)
        .exists()
    )
    if collision:
        raise IdentityCorrectionConflict()


def _apply_reviewed_to_authoritative(document, profile):
    """Promote staged reviewed_* values to authoritative stores. Only fields
    with a non-NULL reviewed_* change. Recomposes full_name when a name
    component changed. Returns (changed_profile_fields, changed_document_fields)."""
    changed_profile = {}
    for field in PROFILE_FIELDS:
        staged = getattr(document, f"reviewed_{field}", None)
        if not staged:
            continue
        current = _as_text(getattr(profile, field))
        if _as_text(staged) != current:
            setattr(profile, field, staged)
            changed_profile[field] = staged
    if NAME_FIELDS.intersection(changed_profile):
        profile.full_name = " ".join(
            p
            for p in (
                profile.given_name,
                profile.father_name,
                profile.grandfather_name,
            )
            if p
        )
    changed_document = {}
    for field in DOCUMENT_FIELDS:
        staged = getattr(document, f"reviewed_{field}", None)
        if not staged:
            continue
        current = _as_text(getattr(document, field))
        if _as_text(staged) != current:
            setattr(document, field, staged)
            changed_document[field] = staged
    return changed_profile, changed_document


def _validate_correction_payload(corrections):
    """Validate + normalize the full correction payload (whitelist + rules)."""
    from django.core.exceptions import ValidationError

    if not isinstance(corrections, dict) or not corrections:
        raise ValidationError("At least one field correction is required.")
    unknown = set(corrections) - REVIEWABLE_FIELDS
    if unknown:
        raise ValidationError(
            {field: ["This field is not editable."] for field in sorted(unknown)}
        )
    normalized = {}
    for field, raw in corrections.items():
        normalized[field] = validate_review_value(field, raw)
    return normalized


def _staged_document(document, corrections, profile):
    """Apply normalized corrections to the reviewed_* staging columns and
    record provenance for changed fields. An EMPTY reviewed value means no
    correction is staged for that field (fields cannot be cleared below the
    authoritative original). Returns list of (field, original, reviewed)."""
    changed = []
    for field, value in corrections.items():
        original = _original_value(document, profile, field)
        reviewed = _typed_value(field, value)
        reviewed_text = _as_text(reviewed)
        if not reviewed_text:
            # Empty staged value: no correction (original stays authoritative).
            setattr(document, f"reviewed_{field}", "")
            continue
        if reviewed_text != original:
            changed.append((field, original, reviewed_text))
        setattr(document, f"reviewed_{field}", reviewed)
    return changed


def update_identity_review_fields(*, actor, document, corrections, review_version):
    """Save reviewer corrections for a PENDING identity. Staged only; does NOT
    approve. Atomic, authorized, whitelisted, validated, provenance recorded."""
    if not can_verify_identity(actor):
        raise VerificationAgentRequired()
    with transaction.atomic():
        document = (
            IdentityDocument.objects.select_for_update()
            .select_related("patient")
            .get(pk=document.pk)
        )
        profile = PatientProfile.objects.select_for_update().get(
            pk=document.patient_id
        )
        if (
            document.verification_status
            != IdentityDocument.VerificationStatus.PENDING
            or document.status != IdentityDocument.LifecycleStatus.CURRENT
        ):
            raise IdentityTransitionConflict()
        if int(review_version) != document.review_version:
            raise StaleReviewConflict()

        normalized = _validate_correction_payload(corrections)
        changed = _staged_document(document, normalized, profile)

        document.review_version += 1
        reviewed_fields = tuple(
            f"reviewed_{field}" for field in REVIEWABLE_FIELDS
        )
        document.save(update_fields=reviewed_fields + ("review_version", "updated_at"))

        for field, original, reviewed in changed:
            IdentityFieldCorrection.objects.create(
                document=document,
                field=field,
                original_value=original,
                reviewed_value=reviewed,
                source=IdentityFieldCorrection.Source.REVIEWER_CORRECTION,
                review_version=document.review_version,
                corrected_by=actor,
            )
        _record_event(
            document,
            IdentityDocumentEvent.EventType.REVIEW_FIELDS_CORRECTED,
            actor,
        )
        record_audit(
            action=AuditLog.Action.IDENTITY_REVIEW_FIELDS_CORRECTED,
            actor=actor,
            patient=profile,
            resource_type="IDENTITY_DOCUMENT",
            resource_uuid=document.uuid,
            new_values={
                "review_version": document.review_version,
                "fields": sorted(f for f, _, _ in changed),
            },
        )
        return document


def correct_verified_identity(
    *, actor, document, corrections, reason_category, note="", review_version
):
    """Correct fields of an already-VERIFIED identity. High-risk action:
    requires a non-blank reason, applies reviewed values to authoritative
    stores immediately, records provenance, emits events/audit, re-evaluates
    dependent guardian evidence. The identity REMAINS VERIFIED."""
    if not can_verify_identity(actor):
        raise VerificationAgentRequired()
    if reason_category not in _REASON_CATEGORIES:
        from django.core.exceptions import ValidationError

        raise ValidationError("A valid correction reason is required.")
    with transaction.atomic():
        document = (
            IdentityDocument.objects.select_for_update()
            .select_related("patient")
            .get(pk=document.pk)
        )
        profile = PatientProfile.objects.select_for_update().get(
            pk=document.patient_id
        )
        if (
            document.verification_status
            != IdentityDocument.VerificationStatus.VERIFIED
            or document.status != IdentityDocument.LifecycleStatus.CURRENT
        ):
            raise IdentityTransitionConflict()
        if int(review_version) != document.review_version:
            raise StaleReviewConflict()

        normalized = _validate_correction_payload(corrections)
        changed = _staged_document(document, normalized, profile)
        if not changed:
            from django.core.exceptions import ValidationError

            raise ValidationError("No field values differ from the current identity.")

        document.review_version += 1
        # Apply directly to authoritative stores (already verified).
        changed_profile, changed_document = _apply_reviewed_to_authoritative(
            document, profile
        )
        if changed_profile:
            profile.save()
        if changed_document:
            _check_correction_conflicts(document, profile)
        reviewed_fields = tuple(
            f"reviewed_{field}" for field in REVIEWABLE_FIELDS
        )
        document.save(
            update_fields=reviewed_fields
            + tuple(DOCUMENT_FIELDS)
            + ("review_version", "updated_at")
        )

        for field, original, reviewed in changed:
            IdentityFieldCorrection.objects.create(
                document=document,
                field=field,
                original_value=original,
                reviewed_value=reviewed,
                source=IdentityFieldCorrection.Source.VERIFIED_CORRECTION,
                review_version=document.review_version,
                corrected_by=actor,
                reason_category=reason_category,
                note=(note or "").strip()[:500],
            )
        _record_event(
            document,
            IdentityDocumentEvent.EventType.VERIFIED_FIELDS_CORRECTED,
            actor,
        )
        record_audit(
            action=AuditLog.Action.IDENTITY_VERIFIED_FIELDS_CORRECTED,
            actor=actor,
            patient=profile,
            resource_type="IDENTITY_DOCUMENT",
            resource_uuid=document.uuid,
            new_values={
                "review_version": document.review_version,
                "fields": sorted(f for f, _, _ in changed),
                "reason_category": reason_category,
            },
        )
        from guardians.services import revalidate_relationships_for_identity

        revalidate_relationships_for_identity(patient=profile, actor=actor)
        return document
