import io

import pytest
from django.core.management import call_command
from django.test import override_settings
from PIL import Image

from audit.models import AuditLog
from documents.models import MedicalDocumentEvent, StoredFile
from documents.services import create_medical_document, verify_stored_file_integrity
from tests.test_medical_documents_api import patient_user

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def medical_storage(tmp_path):
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        yield


def _upload(name, content):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(name, content, content_type="image/png")


def create_real_document(api_client, user, patient, tmp_path, *, color="purple"):
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        upload = _upload("clinical.png", _png(color))
        document = create_medical_document(
            patient=patient,
            actor=user,
            upload=upload,
            metadata={"document_type": "LABORATORY"},
        )
    return document


def _png(color):
    output = io.BytesIO()
    Image.new("RGB", (2, 2), color).save(output, format="PNG")
    return output.getvalue()


def corrupt_blob(stored_file):
    with stored_file.file.open("wb") as handle:
        handle.write(b"tampered-bytes-0123456789")


def test_valid_integrity_untouched_file(api_client, tmp_path):
    user, patient = patient_user()
    document = create_real_document(api_client, user, patient, tmp_path)
    stored = document.stored_file
    stored.integrity_status = StoredFile.IntegrityStatus.PENDING
    stored.save(update_fields=("integrity_status", "updated_at"))
    verified = verify_stored_file_integrity(stored)
    assert verified.integrity_status == StoredFile.IntegrityStatus.VALID
    assert verified.sha256 == verified.sha256
    assert AuditLog.objects.filter(
        action=AuditLog.Action.FILE_INTEGRITY_CHECKED,
        patient=patient,
        resource_uuid=document.uuid,
        new_values={"integrity_status": "VALID"},
    ).exists()


def test_corrupted_blob_marks_corrupted_blocks_download_and_audits(
    api_client, tmp_path
):
    user, patient = patient_user()
    document = create_real_document(api_client, user, patient, tmp_path)
    stored = document.stored_file
    corrupt_blob(stored)
    verified = verify_stored_file_integrity(stored, actor=user)
    assert verified.integrity_status == StoredFile.IntegrityStatus.CORRUPTED

    api_client.force_authenticate(user=user)
    download = api_client.get(f"/api/v1/documents/{document.uuid}/file/")
    # M6-established controlled block for unavailable files (409).
    assert download.status_code == 409

    assert MedicalDocumentEvent.objects.filter(
        document=document,
        event_type=MedicalDocumentEvent.EventType.FILE_INTEGRITY_CHECKED,
    ).exists()
    assert AuditLog.objects.filter(
        action=AuditLog.Action.INTEGRITY_FAILURE,
        patient=patient,
        resource_uuid=document.uuid,
        new_values={"integrity_status": "CORRUPTED"},
    ).exists()


def test_missing_blob_marks_missing_blocks_download_and_audits(api_client, tmp_path):
    user, patient = patient_user()
    document = create_real_document(api_client, user, patient, tmp_path)
    stored = document.stored_file
    stored.file.storage.delete(stored.file.name)
    verified = verify_stored_file_integrity(stored, actor=user)
    assert verified.integrity_status == StoredFile.IntegrityStatus.MISSING

    api_client.force_authenticate(user=user)
    download = api_client.get(f"/api/v1/documents/{document.uuid}/file/")
    assert download.status_code == 409

    assert AuditLog.objects.filter(
        action=AuditLog.Action.INTEGRITY_FAILURE,
        patient=patient,
        resource_uuid=document.uuid,
        new_values={"integrity_status": "MISSING"},
    ).exists()


def test_integrity_verification_never_modifies_bytes(api_client, tmp_path):
    user, patient = patient_user()
    document = create_real_document(api_client, user, patient, tmp_path)
    stored = document.stored_file
    with stored.file.open("rb") as handle:
        original_bytes = handle.read()
    verify_stored_file_integrity(stored)
    with stored.file.open("rb") as handle:
        assert handle.read() == original_bytes


def test_batch_integrity_command_counts_and_safe_output(api_client, tmp_path, capsys):
    user, patient = patient_user()
    valid_doc = create_real_document(api_client, user, patient, tmp_path, color="red")
    corrupted_doc = create_real_document(
        api_client, user, patient, tmp_path, color="green"
    )
    missing_doc = create_real_document(
        api_client, user, patient, tmp_path, color="blue"
    )
    corrupt_blob(corrupted_doc.stored_file)
    missing_doc.stored_file.file.storage.delete(missing_doc.stored_file.file.name)

    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        call_command("verify_medical_file_integrity", "--limit", "10")

    valid_doc.stored_file.refresh_from_db()
    corrupted_doc.stored_file.refresh_from_db()
    missing_doc.stored_file.refresh_from_db()
    assert valid_doc.stored_file.integrity_status == "VALID"
    assert corrupted_doc.stored_file.integrity_status == "CORRUPTED"
    assert missing_doc.stored_file.integrity_status == "MISSING"

    output = capsys.readouterr().out
    assert "VALID: 1" in output
    assert "CORRUPTED: 1" in output
    assert "MISSING: 1" in output
    # No filenames/content printed.
    assert "clinical.png" not in output
    assert "tampered" not in output


def test_integrity_command_scoped_by_document(api_client, tmp_path):
    user, patient = patient_user()
    document = create_real_document(api_client, user, patient, tmp_path)
    corrupt_blob(document.stored_file)
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        call_command("verify_medical_file_integrity", "--document", str(document.uuid))
    document.stored_file.refresh_from_db()
    assert document.stored_file.integrity_status == "CORRUPTED"
