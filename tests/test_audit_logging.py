"""
Static review: audit that production code never logs sensitive payloads.

Scans the application source (excluding tests/migrations) for logging calls
that embed request bodies, validated payloads, raw document text, OCR text,
credentials, or identity document numbers. This is a regression guard over the
M14 "logging / error-leakage" review — sensitive values must stay out of logs.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.django_db

SOURCE_DIRS = [
    "accounts",
    "audit",
    "archive",
    "claims",
    "common",
    "config",
    "documents",
    "facilities",
    "guardians",
    "identities",
    "patients",
    "processing",
]

LOGGER_CALL = re.compile(r"logger\.(debug|info|warning|error|exception|critical)\s*\(")

# Substrings that must never appear inside a logger call's first argument.
FORBIDDEN_IN_LOG_ARG = (
    "request.data",
    "request.body",
    "validated_data",
    ".ocr_text",
    ".native_text",
    "candidate_context",
    "password",
    "access_token",
    "refresh_token",
    "activation_token",
    "sha256",
    "storage_key",
    "document_number",
    "national_number",
    "family_number",
    "file_content",
    "extracted_text",
    "text_content",
)


def _source_files():
    root = Path(__file__).resolve().parents[1]
    for dirname in SOURCE_DIRS:
        base = root / dirname
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if "migrations" in path.parts:
                continue
            yield path


def test_no_sensitive_logging_calls_in_production_source():
    violations = []
    for path in _source_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not LOGGER_CALL.search(line):
                continue
            for forbidden in FORBIDDEN_IN_LOG_ARG:
                if forbidden in line:
                    violations.append(
                        f"{path.relative_to(path.parents[1])}:{lineno}: "
                        f"{forbidden!r} in logger call"
                    )
                    break
    assert not violations, "\n".join(violations)


def test_error_envelope_never_contains_tracebacks_or_internals(api_client):
    """A forced 500-style validation error yields a clean {error: ...} envelope."""
    from tests.test_medical_documents_api import COLLECTION, patient_user

    user, _ = patient_user(email="log-envelope@example.com")
    api_client.force_authenticate(user=user)
    # Missing required fields -> controlled 400 envelope, no internals.
    response = api_client.post(COLLECTION, {}, format="multipart")
    assert response.status_code == 400, response.content
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert "Traceback" not in response.content.decode()
