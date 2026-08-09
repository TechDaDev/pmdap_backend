import pytest
from django.core.exceptions import ValidationError

from audit.admin import AuditLogAdmin
from audit.models import AuditLog
from tests.test_medical_documents_api import patient_user

pytestmark = pytest.mark.django_db


def test_audit_log_create_and_action_enum(api_client):
    user, patient = patient_user()
    entry = AuditLog.objects.create(
        action=AuditLog.Action.DOCUMENT_UPLOADED,
        actor=user,
        patient=patient,
        resource_type="MEDICAL_DOCUMENT",
        new_values={"document_type": "LABORATORY"},
    )
    assert entry.uuid
    assert entry.action == "DOCUMENT_UPLOADED"
    assert entry.created_at is not None
    # Required security/history actions exist.
    required_actions = {
        "ACCOUNT_CREATED",
        "ACCOUNT_ACTIVATED",
        "ACCOUNT_CLAIM_SUBMITTED",
        "ACCOUNT_CLAIM_APPROVED",
        "ACCOUNT_CLAIM_REJECTED",
        "PATIENT_ACCOUNT_LINKED",
        "IDENTITY_DOCUMENT_UPLOADED",
        "IDENTITY_DOCUMENT_VERIFIED",
        "IDENTITY_DOCUMENT_REJECTED",
        "IDENTITY_DOCUMENT_REPLACED",
        "PATIENT_IDENTITY_STATUS_CHANGED",
        "MINOR_CREATED",
        "GUARDIAN_RELATIONSHIP_SUBMITTED",
        "GUARDIAN_RELATIONSHIP_VERIFIED",
        "GUARDIAN_RELATIONSHIP_REJECTED",
        "GUARDIAN_RELATIONSHIP_ENDED",
        "GUARDIAN_ACCESS_EXPIRED",
        "CLAIM_SUBMITTED",
        "CLAIM_MORE_INFORMATION_REQUIRED",
        "CLAIM_APPROVED",
        "CLAIM_REJECTED",
        "ACCOUNT_ACTIVATION_CREATED",
        "DOCUMENT_UPLOADED",
        "DOCUMENT_METADATA_UPDATED",
        "DOCUMENT_DELETED",
        "DOCUMENT_TYPE_CHANGED",
        "DOCUMENT_FACILITY_CHANGED",
        "FILE_INTEGRITY_CHECKED",
        "DATE_CONFIRMED",
        "DATE_CORRECTED",
        "PDF_EXTRACTION_FAILED",
        "OCR_FAILED",
        "INTEGRITY_FAILURE",
    }
    assert required_actions <= set(AuditLog.Action.values)


def test_audit_log_instance_update_is_rejected():
    entry = AuditLog.objects.create(action=AuditLog.Action.DOCUMENT_UPLOADED)
    entry.metadata = {"tampered": True}
    with pytest.raises(ValidationError):
        entry.save()


def test_audit_log_delete_is_rejected():
    entry = AuditLog.objects.create(action=AuditLog.Action.DOCUMENT_UPLOADED)
    with pytest.raises(ValidationError):
        entry.delete()
    assert AuditLog.objects.filter(pk=entry.pk).exists()


def test_audit_log_admin_is_read_only():
    admin_instance = AuditLogAdmin(AuditLog, None)
    request = type("Request", (), {})()
    assert admin_instance.has_add_permission(request) is False
    assert admin_instance.has_change_permission(request, None) is False
    assert admin_instance.has_delete_permission(request, None) is False


def test_audit_log_indexes_exist():
    index_names = {index.name for index in AuditLog._meta.indexes}
    assert {
        "audit_patient_created_idx",
        "audit_actor_created_idx",
        "audit_action_created_idx",
        "audit_resource_idx",
    }.issubset(index_names)
