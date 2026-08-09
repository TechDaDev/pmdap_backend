from datetime import date

import pytest

from accounts.services import register_account
from audit.models import AuditLog
from documents.date_services import confirm_document_date
from tests.test_minors_guardians import create_verified_guardian

pytestmark = pytest.mark.django_db

# Any of these appearing in audit payloads (previous/new/metadata) is a
# privacy/secret leak: credentials, hashes, storage paths, document numbers,
# or raw medical text.
FORBIDDEN_SUBSTRINGS = (
    "password",
    "token",
    "activation",
    "sha256",
    "storage_key",
    "private/identity",
    "private/medical",
    "CARD-",
    "NAT-",
    "FAM-",
    "hemoglobin",
    "report content",
)


def audit_blob():
    parts = []
    for entry in AuditLog.objects.all():
        parts.append(str(entry.previous_values))
        parts.append(str(entry.new_values))
        parts.append(str(entry.metadata))
    return " ".join(parts)


@pytest.fixture(autouse=True)
def private_identity_storage(settings, tmp_path):
    settings.IDENTITY_FILE_ROOT = tmp_path / "private-identity"
    settings.IDENTITY_FILE_MAX_BYTES = 1024 * 1024


def test_audit_payloads_contain_no_secrets_after_flows(api_client):
    # Registration carries a real password + patient profile.
    register_account(
        email="audit-secret@example.com",
        password="A-complex-password-2026!",
        patient={
            "full_name": "Secret Audit",
            "date_of_birth": date(1990, 1, 2),
            "sex": "UNSPECIFIED",
            "nationality": "IQ",
        },
    )
    # Identity submission/approval carries national/family/document numbers.
    create_verified_guardian(
        email="audit-secret-guardian@example.com", family="FAM-SECRET"
    )
    # Medical document + date confirmation.
    from processing.date_services import process_date_candidates
    from tests.test_date_processing import prepared_document

    document = prepared_document(
        "Report Date: 14/03/2026\nHemoglobin 12.4 g/dL\nreport content here\n"
    )
    actor = document.patient.user
    process_date_candidates(str(document.uuid))
    candidate = document.date_candidates.get(is_current=True, is_suggested=True)
    confirm_document_date(document=document, actor=actor, candidate_id=candidate.uuid)

    blob = audit_blob()
    for forbidden in FORBIDDEN_SUBSTRINGS:
        assert forbidden.lower() not in blob.lower(), (
            f"forbidden substring leaked into audit payload: {forbidden!r}"
        )
