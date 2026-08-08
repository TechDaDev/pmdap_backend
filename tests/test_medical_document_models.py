from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from accounts.models import User
from documents.models import MedicalDocument, MedicalDocumentEvent, StoredFile
from patients.models import PatientProfile

pytestmark = pytest.mark.django_db


def make_user(email="uploader@example.com"):
    return User.objects.create_user(
        email=email,
        password="A-complex-password-2026!",
        status=User.Status.ACTIVE,
    )


def make_patient(*, digital_id="12345678901234567", user=None):
    return PatientProfile.objects.create(
        user=user,
        digital_id=digital_id,
        full_name="Medical Document Patient",
        date_of_birth=date(1990, 1, 2),
        sex=PatientProfile.Sex.UNSPECIFIED,
        nationality="IQ",
    )


def make_file(*, digest="a" * 64, name="medical/file.pdf"):
    return StoredFile.objects.create(
        file=name,
        original_filename="report.pdf",
        mime_type="application/pdf",
        size_bytes=123,
        sha256=digest,
        page_count=2,
    )


def make_document(*, patient, uploader, digest="a" * 64):
    return MedicalDocument.objects.create(
        patient=patient,
        uploaded_by=uploader,
        stored_file=make_file(digest=digest, name=f"medical/{digest}.pdf"),
        content_sha256=digest,
        document_type=MedicalDocument.DocumentType.LABORATORY,
    )


def test_stored_file_defaults_are_truthful_and_evidence_is_immutable():
    stored = make_file()

    assert stored.integrity_status == StoredFile.IntegrityStatus.PENDING
    assert stored.malware_scan_status == StoredFile.MalwareScanStatus.NOT_CONFIGURED

    stored.sha256 = "b" * 64
    with pytest.raises(ValidationError, match="evidence is immutable"):
        stored.save()


def test_medical_document_defaults_and_deterministic_ordering():
    uploader = make_user()
    patient = make_patient(user=uploader)
    document = make_document(patient=patient, uploader=uploader)

    assert document.processing_status == MedicalDocument.ProcessingStatus.UPLOADED
    assert document.archive_status == MedicalDocument.ArchiveStatus.ACTIVE
    assert document.document_date is None
    assert document.date_source == ""
    assert document.date_verified is False
    assert MedicalDocument._meta.ordering == ("-created_at", "-uuid")


def test_document_type_and_date_source_are_controlled_values():
    uploader = make_user()
    patient = make_patient(user=uploader)
    document = make_document(patient=patient, uploader=uploader)
    document.document_type = "EXECUTABLE"
    document.date_source = "AUTO_GUESSED"

    with pytest.raises(ValidationError) as exc_info:
        document.full_clean()

    assert set(exc_info.value.message_dict) >= {"document_type", "date_source"}


def test_active_duplicate_is_unique_per_patient():
    uploader = make_user()
    patient = make_patient(user=uploader)
    make_document(patient=patient, uploader=uploader)

    with pytest.raises(IntegrityError), transaction.atomic():
        make_document(patient=patient, uploader=uploader)


def test_same_content_is_allowed_for_different_patients():
    uploader = make_user()
    first = make_patient(user=uploader)
    second = make_patient(digital_id="76543210987654321")

    make_document(patient=first, uploader=uploader)
    make_document(patient=second, uploader=uploader)

    assert MedicalDocument.objects.count() == 2


def test_soft_deleted_document_does_not_block_reupload():
    uploader = make_user()
    patient = make_patient(user=uploader)
    first = make_document(patient=patient, uploader=uploader)
    first.archive_status = MedicalDocument.ArchiveStatus.DELETED
    first.save(update_fields=("archive_status", "updated_at"))

    make_document(patient=patient, uploader=uploader)

    assert MedicalDocument.objects.filter(patient=patient).count() == 2


def test_medical_document_events_are_append_only():
    uploader = make_user()
    document = make_document(
        patient=make_patient(user=uploader),
        uploader=uploader,
    )
    event = MedicalDocumentEvent.objects.create(
        document=document,
        event_type=MedicalDocumentEvent.EventType.UPLOADED,
        actor=uploader,
    )

    event.metadata = {"changed": True}
    with pytest.raises(ValidationError, match="immutable"):
        event.save()
    with pytest.raises(ValidationError, match="immutable"):
        event.delete()
