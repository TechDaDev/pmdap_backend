import pytest

from audit.models import AuditLog
from audit.services import (
    REDACTED,
    current_request_id,
    record_audit,
    sanitize_audit_values,
    set_request_id,
)
from tests.test_medical_documents_api import patient_user

pytestmark = pytest.mark.django_db


def test_sanitize_audit_values_redacts_sensitive_keys():
    payload = {
        "password": "secret",
        "token_hash": "abc",
        "national_number": "123",
        "document_number": "CARD-1",
        "file_path": "/var/lib/private/x.png",
        "sha256": "a" * 64,
        "safe_field": "LABORATORY",
        "nested": {"password_hash": "x", "ok": "y"},
        "items": [{"text": "report content", "kind": "report"}],
    }
    sanitized = sanitize_audit_values(payload)
    assert sanitized["password"] == REDACTED
    assert sanitized["token_hash"] == REDACTED
    assert sanitized["national_number"] == REDACTED
    assert sanitized["document_number"] == REDACTED
    assert sanitized["file_path"] == REDACTED
    assert sanitized["sha256"] == REDACTED
    assert sanitized["safe_field"] == "LABORATORY"
    assert sanitized["nested"]["password_hash"] == REDACTED
    assert sanitized["nested"]["ok"] == "y"
    assert sanitized["items"][0]["text"] == REDACTED


def test_record_audit_persists_sanitized_and_request_id():
    user, patient = patient_user()
    set_request_id("req-123")
    try:
        entry = record_audit(
            action=AuditLog.Action.DOCUMENT_UPLOADED,
            actor=user,
            patient=patient,
            resource_type="MEDICAL_DOCUMENT",
            new_values={"title": "CBC", "password": "hunter2"},
            metadata={"fields": ["title"]},
        )
    finally:
        set_request_id("")
    assert entry.request_id == "req-123"
    assert entry.new_values["title"] == "CBC"
    assert entry.new_values["password"] == REDACTED
    assert AuditLog.objects.filter(uuid=entry.uuid).exists()


def test_current_request_id_thread_local_defaults_blank():
    set_request_id("")
    assert current_request_id() == ""


def test_record_audit_system_actor_type():
    entry = record_audit(
        action=AuditLog.Action.PDF_EXTRACTION_FAILED,
        actor_type=AuditLog.ActorType.SYSTEM,
        resource_type="MEDICAL_DOCUMENT",
        new_values={"processing_status": "FAILED"},
    )
    assert entry.actor_type == "SYSTEM"
    assert entry.actor is None
