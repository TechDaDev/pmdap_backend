import uuid
from dataclasses import dataclass

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from claims.models import (
    ClaimIdentityEvidence,
    PatientAccountClaim,
    PatientAccountClaimEvent,
)
from identities.models import IdentityDocument
from identities.services import (
    delete_identity_file_from_storage,
    persist_identity_upload,
)
from patients.models import PatientProfile

User = get_user_model()


@dataclass(frozen=True)
class ClaimReceipt:
    claim_id: uuid.UUID
    status: str = PatientAccountClaim.Status.PENDING


def _comparison(left, right):
    if left in (None, "") or right in (None, ""):
        return PatientAccountClaim.Comparison.UNAVAILABLE
    return (
        PatientAccountClaim.Comparison.MATCH
        if str(left).strip().casefold() == str(right).strip().casefold()
        else PatientAccountClaim.Comparison.MISMATCH
    )


def _eligible_profile(data):
    profile = PatientProfile.objects.filter(digital_id=data["digital_id"]).first()
    if (
        profile is None
        or profile.user_id is not None
        or profile.is_minor
        or profile.identity_status != PatientProfile.IdentityStatus.VERIFIED
        or User.objects.filter(email__iexact=data["email"]).exists()
        or PatientAccountClaim.objects.filter(
            patient=profile, status__in=PatientAccountClaim.ACTIVE_STATUSES
        ).exists()
        or PatientAccountClaim.objects.filter(
            requested_email__iexact=data["email"],
            status__in=PatientAccountClaim.ACTIVE_STATUSES,
        ).exists()
    ):
        return None, None
    document = IdentityDocument.objects.filter(
        patient=profile,
        document_type=IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD,
        status=IdentityDocument.LifecycleStatus.CURRENT,
        verification_status=IdentityDocument.VerificationStatus.VERIFIED,
    ).first()
    return (profile, document) if document else (None, None)


def submit_account_claim(validated_data):
    data = dict(validated_data)
    profile, current_document = _eligible_profile(data)
    if profile is None:
        return ClaimReceipt(uuid.uuid4())

    files = []
    try:
        with transaction.atomic():
            front = persist_identity_upload(data.pop("front_image"))
            files.append(front)
            back = persist_identity_upload(data.pop("back_image"))
            files.append(back)
            claim = PatientAccountClaim.objects.create(
                patient=profile,
                requested_email=data["email"],
                requested_phone=data["phone"],
                submitted_name=data["full_name"],
                submitted_date_of_birth=data["date_of_birth"],
                name_comparison=_comparison(data["full_name"], profile.full_name),
                date_of_birth_comparison=_comparison(
                    data["date_of_birth"], profile.date_of_birth
                ),
                document_number_comparison=_comparison(
                    data["identity_document_number"], current_document.document_number
                ),
            )
            ClaimIdentityEvidence.objects.create(
                claim=claim,
                document_type=ClaimIdentityEvidence.DocumentType.UNIFIED_NATIONAL_CARD,
                document_number=data["identity_document_number"],
                issuing_country="IQ",
                front_image=front,
                back_image=back,
            )
            if data.get("passport_number"):
                passport_front = persist_identity_upload(data["passport_front_image"])
                files.append(passport_front)
                passport_back = None
                if data.get("passport_back_image"):
                    passport_back = persist_identity_upload(data["passport_back_image"])
                    files.append(passport_back)
                ClaimIdentityEvidence.objects.create(
                    claim=claim,
                    document_type=ClaimIdentityEvidence.DocumentType.PASSPORT,
                    document_number=data["passport_number"],
                    issuing_country=data["passport_issuing_country"],
                    issue_date=data["passport_issue_date"],
                    expiry_date=data["passport_expiry_date"],
                    front_image=passport_front,
                    back_image=passport_back,
                )
            PatientAccountClaimEvent.objects.create(
                claim=claim,
                event_type=PatientAccountClaimEvent.EventType.SUBMITTED,
                metadata={},
            )
        return ClaimReceipt(claim.uuid)
    except IntegrityError:
        for identity_file in files:
            delete_identity_file_from_storage(identity_file)
        return ClaimReceipt(uuid.uuid4())
    except Exception:
        for identity_file in files:
            delete_identity_file_from_storage(identity_file)
        raise
