import hashlib
import io
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.test import override_settings
from PIL import Image

from accounts.models import User
from documents.exceptions import (
    DuplicateMedicalDocument,
    MedicalDocumentNotFound,
    MedicalFileStorageFailed,
)
from documents.models import MedicalDocument, MedicalDocumentEvent, StoredFile
from documents.services import (
    create_medical_document,
    soft_delete_medical_document,
    update_medical_document,
    verify_stored_file_integrity,
)
from patients.models import PatientProfile

pytestmark = pytest.mark.django_db


def png_bytes():
    output = io.BytesIO()
    Image.new("RGB", (2, 2), "green").save(output, format="PNG")
    return output.getvalue()


def upload(content=None, name="clinical.png"):
    return SimpleUploadedFile(
        name,
        content or png_bytes(),
        content_type="image/png",
    )


def actor_and_patient():
    actor = User.objects.create_user(
        email="medical-owner@example.com",
        password="A-complex-password-2026!",
        status=User.Status.ACTIVE,
    )
    patient = PatientProfile.objects.create(
        user=actor,
        digital_id="12345678901234567",
        full_name="Medical Owner",
        date_of_birth=date(1990, 1, 2),
        sex=PatientProfile.Sex.UNSPECIFIED,
        nationality="IQ",
    )
    return actor, patient


def create(*, patient, actor, file=None, **metadata):
    return create_medical_document(
        patient=patient,
        actor=actor,
        upload=file or upload(),
        metadata={
            "document_type": MedicalDocument.DocumentType.LABORATORY,
            **metadata,
        },
    )


def test_upload_persists_exact_original_and_lifecycle_evidence(tmp_path):
    actor, patient = actor_and_patient()
    content = png_bytes()
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        document = create(
            patient=patient,
            actor=actor,
            file=upload(content),
            title="Blood count",
            document_date=date(2026, 8, 1),
        )

        assert Path(document.stored_file.file.path).read_bytes() == content

    assert document.patient == patient
    assert document.uploaded_by == actor
    assert document.title == "Blood count"
    assert document.date_source == MedicalDocument.DateSource.USER_ENTERED
    assert document.date_verified is True
    assert document.date_verified_at is not None
    assert document.stored_file.malware_scan_status == "NOT_CONFIGURED"
    assert document.stored_file.integrity_status == StoredFile.IntegrityStatus.VALID
    assert document.events.get().event_type == MedicalDocumentEvent.EventType.UPLOADED
    assert "/" in document.stored_file.file.name
    assert "clinical" not in document.stored_file.file.name


def test_duplicate_rejected_before_new_blob_and_records_event(tmp_path):
    actor, patient = actor_and_patient()
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        first = create(patient=patient, actor=actor)
        files_before = sorted(path for path in tmp_path.rglob("*") if path.is_file())

        with pytest.raises(DuplicateMedicalDocument):
            create(
                patient=patient,
                actor=actor,
                file=upload(name="renamed-same-content.png"),
            )

        files_after = sorted(path for path in tmp_path.rglob("*") if path.is_file())
        assert files_after == files_before

    assert StoredFile.objects.count() == 1
    assert MedicalDocument.objects.count() == 1
    assert first.events.filter(
        event_type=MedicalDocumentEvent.EventType.DUPLICATE_REJECTED
    ).exists()


def test_database_failure_removes_new_blob_and_rolls_back_rows(tmp_path):
    actor, patient = actor_and_patient()
    with (
        override_settings(MEDICAL_FILE_ROOT=tmp_path),
        patch.object(
            MedicalDocument.objects,
            "create",
            side_effect=IntegrityError("document database failure"),
        ),
        pytest.raises(IntegrityError, match="document database failure"),
    ):
        create(patient=patient, actor=actor)

    assert not [path for path in tmp_path.rglob("*") if path.is_file()]
    assert not StoredFile.objects.exists()


def test_integrity_check_records_valid_then_detects_tampering(tmp_path):
    actor, patient = actor_and_patient()
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        document = create(patient=patient, actor=actor)
        stored = verify_stored_file_integrity(document.stored_file, actor=actor)
        assert stored.integrity_status == StoredFile.IntegrityStatus.VALID

        Path(stored.file.path).write_bytes(b"tampered")
        stored = verify_stored_file_integrity(stored, actor=actor)

    assert stored.integrity_status == StoredFile.IntegrityStatus.CORRUPTED
    assert (
        document.events.filter(
            event_type=MedicalDocumentEvent.EventType.FILE_INTEGRITY_CHECKED
        ).count()
        == 2
    )


def test_stale_update_and_repeated_delete_cannot_mutate_deleted_document(tmp_path):
    actor, patient = actor_and_patient()
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        document = create(patient=patient, actor=actor)
        stale = MedicalDocument.objects.get(pk=document.pk)
        soft_delete_medical_document(document=document, actor=actor)

        with pytest.raises(MedicalDocumentNotFound):
            update_medical_document(
                document=stale,
                actor=actor,
                metadata={"title": "Must not change"},
            )
        with pytest.raises(MedicalDocumentNotFound):
            soft_delete_medical_document(document=stale, actor=actor)

    document.refresh_from_db()
    assert document.title == ""
    assert (
        document.events.filter(
            event_type=MedicalDocumentEvent.EventType.DELETED
        ).count()
        == 1
    )


def test_post_write_integrity_mismatch_rolls_back_rows_and_blob(tmp_path):
    actor, patient = actor_and_patient()
    with (
        override_settings(MEDICAL_FILE_ROOT=tmp_path),
        patch("documents.services._stored_digest", return_value="b" * 64),
        pytest.raises(MedicalFileStorageFailed),
    ):
        create(patient=patient, actor=actor)

    assert not StoredFile.objects.exists()
    assert not MedicalDocument.objects.exists()
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


def test_event_failure_rolls_back_document_file_row_and_blob(tmp_path):
    actor, patient = actor_and_patient()
    with (
        override_settings(MEDICAL_FILE_ROOT=tmp_path),
        patch(
            "documents.services._record_event",
            side_effect=RuntimeError("event failed"),
        ),
        pytest.raises(RuntimeError, match="event failed"),
    ):
        create(patient=patient, actor=actor)

    assert not StoredFile.objects.exists()
    assert not MedicalDocument.objects.exists()
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


def test_clearing_manual_date_resets_provenance_and_empty_patch_is_noop(tmp_path):
    actor, patient = actor_and_patient()
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        document = create(
            patient=patient,
            actor=actor,
            document_date=date(2026, 8, 1),
        )
        document = update_medical_document(
            document=document,
            actor=actor,
            metadata={"document_date": None},
        )
        event_count = document.events.count()
        document = update_medical_document(
            document=document,
            actor=actor,
            metadata={},
        )

    assert document.document_date is None
    assert document.date_source == ""
    assert document.date_verified is False
    assert document.date_verified_at is None
    assert document.events.count() == event_count


def test_integrity_service_supports_unattached_stored_file(tmp_path):
    content = png_bytes()
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        stored = StoredFile(
            original_filename="orphan.png",
            mime_type="image/png",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )
        stored.file.save("medical/orphan.png", ContentFile(content), save=False)
        stored.save()
        verified = verify_stored_file_integrity(stored)

    assert verified.integrity_status == StoredFile.IntegrityStatus.VALID
    assert not MedicalDocumentEvent.objects.exists()
