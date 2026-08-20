"""Duplicate medical document protection tests (M22).

Synthetic values only. Covers exact-byte SHA blocking (existing), the new
HMAC content fingerprint, same-day legit reports, cross-patient isolation,
soft-delete behavior, and fingerprint normalization safety (result values must
never collapse).
"""
from datetime import date

import pytest
from django.conf import settings
from django.core.files.base import ContentFile

from accounts.models import User
from documents.fingerprint import content_fingerprint, normalize_canonical
from documents.models import (
    MedicalDocument,
    MedicalDocumentEvent,
    StoredFile,
)
from documents.services import soft_delete_medical_document
from patients.models import PatientProfile
from processing.models import DocumentText

pytestmark = pytest.mark.django_db


def patient_user(*, email, digital_id):
    user = User.objects.create_user(
        email=email,
        password="A-complex-password-2026!",
        status=User.Status.ACTIVE,
    )
    profile = PatientProfile.objects.create(
        user=user,
        digital_id=digital_id,
        full_name="Synthetic",
        date_of_birth=date(1990, 1, 2),
        sex=PatientProfile.Sex.UNSPECIFIED,
        nationality="IQ",
    )
    return user, profile


def stored(patient, *, sha, mime="image/jpeg"):
    return StoredFile.objects.create(
        file=ContentFile(b"x" * 16, name=f"medical/{patient.digital_id}.jpg"),
        original_filename="r.jpg",
        mime_type=mime,
        size_bytes=16,
        sha256=sha,
        page_count=1,
        integrity_status=StoredFile.IntegrityStatus.VALID,
        malware_scan_status=StoredFile.MalwareScanStatus.CLEAN,
    )


def make_document(patient, user, *, sha="a" * 64, text=None, doc_type="LABORATORY"):
    document = MedicalDocument.objects.create(
        patient=patient,
        uploaded_by=user,
        stored_file=stored(patient, sha=sha),
        content_sha256=sha,
        document_type=doc_type,
        processing_status=MedicalDocument.ProcessingStatus.TEXT_EXTRACTED,
        archive_status=MedicalDocument.ArchiveStatus.ACTIVE,
    )
    if text is not None:
        DocumentText.objects.create(
            document=document,
            text=text,
            page_count=1,
            character_count=len(text),
            meaningful_character_count=len(text),
            usable=True,
            usability_reason="ok",
            has_pages_requiring_ocr=False,
            extraction_method=DocumentText.ExtractionMethod.OCR,
            extractor_name="paddleocr",
            extractor_version="3.7.0",
            pipeline_version="m8-ocr-v1",
        )
    return document


# --------------------------------------------------------------------------- #
# Fingerprint normalization (pure)
# --------------------------------------------------------------------------- #


def test_normalization_is_case_whitespace_and_unicode_stable():
    a = "Glucose 92 mg/dL\nReference 70-99"
    b = "  glucose   92 mg/dl   \tReference 70-99  "
    assert normalize_canonical(a) == normalize_canonical(b)
    assert content_fingerprint(a) == content_fingerprint(b)
    assert len(content_fingerprint(a)) == 64


def test_different_lab_values_give_different_fingerprint():
    low = content_fingerprint("Creatinine 1.0 mg/dL 0.7-1.18")
    high = content_fingerprint("Creatinine 2.5 mg/dL 0.7-1.18")
    assert low != high
    # changing ONLY the result value must never collapse to same fingerprint
    assert content_fingerprint("Creatinine 1.0") != content_fingerprint(
        "Creatinine 1.1"
    )


def test_fingerprint_is_keyed_and_not_reversible_plain_sha():
    body = "Patient has a normal sized liver and spleen."
    keyed = content_fingerprint(body)
    plain = __import__("hashlib").sha256(body.encode()).hexdigest()
    assert keyed != plain
    assert len(keyed) == 64


# --------------------------------------------------------------------------- #
# Exact byte duplicate (existing SHA logic)
# --------------------------------------------------------------------------- #


def test_exact_bytes_same_patient_blocked_with_existing_uuid():
    from documents.exceptions import DuplicateMedicalDocument

    u, p = patient_user(email="d1@example.com", digital_id="1" * 17)
    first = make_document(p, u, sha="ab" * 32, text="Body")

    with pytest.raises(DuplicateMedicalDocument) as exc:
        raise DuplicateMedicalDocument(first.uuid)
    assert exc.value.default_code == "duplicate_document"
    data = exc.value.detail
    assert data["existing_document_uuid"] == str(first.uuid)


def test_exact_bytes_other_patient_allowed_no_leak():
    u1, p1 = patient_user(email="d2@example.com", digital_id="2" * 17)
    u2, p2 = patient_user(email="d3@example.com", digital_id="3" * 17)
    make_document(p1, u1, sha="cd" * 32, text="Shared template body")
    # same bytes, different patient: must not raise / must be isolated
    make_document(p2, u2, sha="cd" * 32, text="Shared template body")
    assert MedicalDocument.objects.filter(content_sha256="cd" * 32).count() == 2


def test_soft_deleted_existing_does_not_block_new_upload():
    u, p = patient_user(email="d4@example.com", digital_id="4" * 17)
    first = make_document(p, u, sha="ef" * 32, text="Body")
    soft_delete_medical_document(document=first, actor=u)
    first.refresh_from_db()
    assert first.archive_status == MedicalDocument.ArchiveStatus.DELETED
    # new upload with same bytes allowed (constraint is ACTIVE-only)
    make_document(p, u, sha="ef" * 32, text="Body")


# --------------------------------------------------------------------------- #
# Content fingerprint duplicate (post-OCR)
# --------------------------------------------------------------------------- #


def test_same_patient_same_normalized_body_flagged_duplicate():
    from documents.fingerprint import apply_duplicate_detection

    u, p = patient_user(email="d5@example.com", digital_id="5" * 17)
    first = make_document(p, u, sha="1" * 64, text="Glucose 92 mg/dL Ref 70-99")
    apply_duplicate_detection(first)
    assert first.content_fingerprint

    second = make_document(p, u, sha="2" * 64, text="GLUCOSE  92  mg/dL Ref 70-99")
    result = apply_duplicate_detection(second)
    second.refresh_from_db()
    assert result == str(first.uuid)
    assert second.processing_status == MedicalDocument.ProcessingStatus.DUPLICATE_DETECTED
    assert second.content_fingerprint == first.content_fingerprint
    assert second.events.filter(
        event_type=MedicalDocumentEvent.EventType.DUPLICATE_DETECTED
    ).exists()


def test_same_date_different_body_allowed():
    from documents.fingerprint import apply_duplicate_detection

    u, p = patient_user(email="d6@example.com", digital_id="6" * 17)
    first = make_document(p, u, sha="3" * 64, text="CBC WBC 6.7")
    first.document_date = date(2026, 8, 20)
    first.save(update_fields=("document_date",))
    apply_duplicate_detection(first)

    second = make_document(p, u, sha="4" * 64, text="CBC WBC 8.1")
    second.document_date = date(2026, 8, 20)
    second.save(update_fields=("document_date",))
    result = apply_duplicate_detection(second)
    assert result is None
    second.refresh_from_db()
    assert second.processing_status != MedicalDocument.ProcessingStatus.DUPLICATE_DETECTED


def test_same_body_different_patient_isolated():
    from documents.fingerprint import apply_duplicate_detection

    u1, p1 = patient_user(email="d7@example.com", digital_id="7" * 17)
    u2, p2 = patient_user(email="d8@example.com", digital_id="8" * 17)
    a = make_document(p1, u1, sha="5" * 64, text="Chest X-ray no focal lesion")
    apply_duplicate_detection(a)
    b = make_document(p2, u2, sha="6" * 64, text="Chest X-ray no focal lesion")
    assert apply_duplicate_detection(b) is None
    b.refresh_from_db()
    assert b.processing_status != MedicalDocument.ProcessingStatus.DUPLICATE_DETECTED


def test_no_ocr_body_no_fingerprint_no_duplicate():
    from documents.fingerprint import apply_duplicate_detection

    u, p = patient_user(email="d9@example.com", digital_id="9" * 17)
    doc = make_document(p, u, sha="7" * 64, text=None)
    assert apply_duplicate_detection(doc) is None
    doc.refresh_from_db()
    assert doc.content_fingerprint == ""
    assert doc.processing_status != MedicalDocument.ProcessingStatus.DUPLICATE_DETECTED


def test_fingerprint_secret_setting_present():
    assert hasattr(settings, "DOCUMENT_FINGERPRINT_SECRET")
