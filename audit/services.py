import threading

from audit.models import AuditLog

REDACTED = "[REDACTED]"

# Fields that must never be persisted in normalized audit payloads. The audit
# layer only ever receives allowlisted values; this is a central safety net.
SENSITIVE_KEYS = {
    "password",
    "password_hash",
    "token",
    "access_token",
    "refresh_token",
    "activation_token",
    "activation_token_hash",
    "token_hash",
    "secret",
    "email_verification_token",
    "authorization",
    "national_number",
    "family_number",
    "document_number",
    "sha256",
    "storage_key",
    "file_path",
    "path",
    "file",
    "front_image",
    "back_image",
    "identity_image",
    "text",
    "ocr_text",
    "native_text",
    "candidate_context",
    "context",
    "raw",
}

_request_local = threading.local()


def set_request_id(request_id):
    _request_local.request_id = request_id


def current_request_id():
    return getattr(_request_local, "request_id", "")


def sanitize_audit_values(values):
    """Recursively redact sensitive keys from audit payloads."""
    if values is None:
        return {}
    output = {}
    for key, value in values.items():
        if key in SENSITIVE_KEYS:
            output[key] = REDACTED
        elif isinstance(value, dict):
            output[key] = sanitize_audit_values(value)
        elif isinstance(value, list):
            output[key] = [
                sanitize_audit_values(item) if isinstance(item, dict) else item
                for item in value
            ]
        else:
            output[key] = value
    return output


def record_audit(
    *,
    action,
    actor=None,
    actor_type=AuditLog.ActorType.USER,
    patient=None,
    resource_type="",
    resource_uuid=None,
    previous_values=None,
    new_values=None,
    metadata=None,
):
    return AuditLog.objects.create(
        action=action,
        actor=actor,
        actor_type=actor_type,
        patient=patient,
        resource_type=resource_type,
        resource_uuid=resource_uuid,
        previous_values=sanitize_audit_values(previous_values),
        new_values=sanitize_audit_values(new_values),
        metadata=sanitize_audit_values(metadata),
        request_id=current_request_id(),
    )
