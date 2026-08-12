"""Admin coverage + safety tests.

The Django admin is for operations/inspection only. These tests assert:

  * required models are registered
  * immutable/event admins prohibit add/change/delete
  * extraction-job admin never exposes storage keys
  * processing admins never list sensitive OCR/text fields
  * sensitive identity numbers are never in identity list_display
  * activation admin never lists token_hash
"""
import pytest
from django.contrib import admin as django_admin

from claims.models import (
    AccountActivation,
    ClaimIdentityEvidence,
    PatientAccountClaim,
    PatientAccountClaimEvent,
)
from documents.models import (
    DocumentDateEvent,
    MedicalDocument,
    MedicalDocumentEvent,
    StoredFile,
)
from guardians.models import (
    GuardianEvidence,
    GuardianRelationship,
    GuardianRelationshipEvent,
    MinorCreationRequest,
)
from identities.models import (
    IdentityDocument,
    IdentityDocumentEvent,
    IdentityExtractionJob,
    IdentityFile,
)
from patients.models import PatientProfile
from processing.models import DateCandidate, DocumentText, DocumentTextPage

REGISTERED_MODELS = [
    PatientProfile,
    IdentityDocument,
    IdentityFile,
    IdentityDocumentEvent,
    IdentityExtractionJob,
    GuardianRelationship,
    GuardianEvidence,
    GuardianRelationshipEvent,
    MinorCreationRequest,
    PatientAccountClaim,
    ClaimIdentityEvidence,
    AccountActivation,
    PatientAccountClaimEvent,
    MedicalDocument,
    MedicalDocumentEvent,
    DocumentDateEvent,
    StoredFile,
    DocumentText,
    DocumentTextPage,
    DateCandidate,
]


def admin_for(model):
    return django_admin.site._registry[model]


@pytest.mark.parametrize("model", REGISTERED_MODELS, ids=lambda m: m.__name__)
def test_model_registered_in_admin(model):
    assert django_admin.site.is_registered(model)
    assert admin_for(model) is not None


READ_ONLY_ADMINS = [
    IdentityFile,
    IdentityDocument,
    IdentityDocumentEvent,
    IdentityExtractionJob,
    GuardianRelationship,
    GuardianEvidence,
    GuardianRelationshipEvent,
    MinorCreationRequest,
    PatientAccountClaim,
    ClaimIdentityEvidence,
    AccountActivation,
    PatientAccountClaimEvent,
    StoredFile,
    MedicalDocumentEvent,
    DocumentDateEvent,
    DocumentText,
    DocumentTextPage,
    DateCandidate,
]


@pytest.mark.parametrize("model", READ_ONLY_ADMINS, ids=lambda m: m.__name__)
def test_immutable_admin_forbids_add_change_delete(model):
    admin_class = admin_for(model)
    request = type("Request", (), {})()
    assert admin_class.has_add_permission(request) is False
    assert admin_class.has_change_permission(request, obj=None) is False
    assert admin_class.has_delete_permission(request, obj=None) is False
    assert admin_class.actions is None


def test_patient_admin_locks_verified_identity_fields():
    from patients.models import PatientProfile

    admin_class = admin_for(PatientProfile)

    class Request:
        pass

    class VerifiedPatient:
        identity_status = PatientProfile.IdentityStatus.VERIFIED

    assert "date_of_birth" in admin_class.get_readonly_fields(
        Request(), obj=VerifiedPatient()
    )
    assert "digital_id" in admin_class.get_readonly_fields(Request(), obj=None)
    assert "identity_status" in admin_class.get_readonly_fields(Request(), obj=None)


def test_identity_document_admin_hides_sensitive_numbers_from_list():
    admin_class = admin_for(IdentityDocument)
    for sensitive in ("document_number", "national_number", "family_number"):
        assert sensitive not in admin_class.list_display
    # ...but remains searchable by a privileged superuser.
    assert "document_number" in admin_class.search_fields


def test_identity_document_admin_has_no_verification_transition_fields():
    admin_class = admin_for(IdentityDocument)
    readonly = set(admin_class.get_readonly_fields(type("R", (), {})()))
    assert "verification_status" in readonly
    assert "status" in readonly
    assert "verified_at" in readonly


def test_extraction_job_admin_never_exposes_storage_keys():
    admin_class = admin_for(IdentityExtractionJob)
    exposed = set(admin_class.list_display) | set(admin_class.fields)
    assert "front_key" not in exposed
    assert "back_key" not in exposed


def test_identity_file_admin_has_no_file_field():
    admin_class = admin_for(IdentityFile)
    assert "file" in admin_class.exclude


def test_processing_admins_never_list_or_expose_ocr_text():
    text_admin = admin_for(DocumentText)
    page_admin = admin_for(DocumentTextPage)
    date_admin = admin_for(DateCandidate)

    for sensitive in ("text", "native_text", "ocr_text"):
        assert sensitive not in text_admin.list_display
        assert sensitive not in page_admin.list_display
        assert sensitive not in page_admin.fields
        assert sensitive not in text_admin.fields
    for sensitive in ("raw_value", "normalized_value", "context"):
        assert sensitive not in date_admin.list_display
        assert sensitive not in date_admin.fields


def test_activation_admin_never_lists_token_hash():
    admin_class = admin_for(AccountActivation)
    assert "token_hash" not in admin_class.list_display
    # Redacted display method is used instead.
    assert any(
        getattr(admin_class, name, None) is not None
        and getattr(getattr(admin_class, name), "is_admin_ordering", False)
        or name.endswith("redacted")
        for name in dir(admin_class)
    )


def test_evidence_admins_exclude_private_image_fields():
    identity_file_admin = admin_for(IdentityFile)
    assert "file" in identity_file_admin.exclude
    claim_evidence_admin = admin_for(ClaimIdentityEvidence)
    assert "front_image" in claim_evidence_admin.exclude
    assert "back_image" in claim_evidence_admin.exclude
    guardian_evidence_admin = admin_for(GuardianEvidence)
    assert "file" in guardian_evidence_admin.exclude


def test_stored_file_admin_excludes_file_field():
    admin_class = admin_for(StoredFile)
    assert "file" in admin_class.exclude
