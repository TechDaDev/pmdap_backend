import io
import threading
from datetime import date

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connection
from django.test import override_settings
from PIL import Image

from accounts.models import User
from documents.exceptions import DuplicateMedicalDocument
from documents.models import MedicalDocument, StoredFile
from documents.services import create_medical_document
from patients.models import PatientProfile

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgresql]


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL-only medical document concurrency test")


def run_concurrently(*operations):
    barrier = threading.Barrier(len(operations))
    results = []
    failures = []

    def run(operation):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            results.append(operation())
        except Exception as exc:  # exact outcomes asserted by each scenario
            failures.append(exc)
        finally:
            close_old_connections()

    threads = [
        threading.Thread(target=run, args=(operation,)) for operation in operations
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()
    return results, failures


def content():
    output = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(output, format="PNG")
    return output.getvalue()


def upload():
    return SimpleUploadedFile("report.png", content(), content_type="image/png")


def user_and_patient(*, email, digital_id):
    user = User.objects.create_user(
        email=email,
        password="A-complex-password-2026!",
        status=User.Status.ACTIVE,
    )
    patient = PatientProfile.objects.create(
        user=user,
        digital_id=digital_id,
        full_name="Concurrent Patient",
        date_of_birth=date(1990, 1, 1),
        sex=PatientProfile.Sex.UNSPECIFIED,
        nationality="IQ",
    )
    return user, patient


def operation(user_uuid, patient_uuid):
    def create():
        user = User.objects.get(uuid=user_uuid)
        patient = PatientProfile.objects.get(uuid=patient_uuid)
        return create_medical_document(
            patient=patient,
            actor=user,
            upload=upload(),
            metadata={"document_type": "LABORATORY"},
        ).uuid

    return create


def test_same_patient_concurrent_duplicate_has_one_winner_and_no_orphan(tmp_path):
    require_postgresql()
    user, patient = user_and_patient(
        email="concurrent@example.com",
        digital_id="60000000000000001",
    )
    create = operation(user.uuid, patient.uuid)

    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        results, failures = run_concurrently(create, create)

    assert len(results) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], DuplicateMedicalDocument)
    assert MedicalDocument.objects.filter(patient=patient).count() == 1
    assert StoredFile.objects.count() == 1
    blobs = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert len(blobs) == 1


def test_same_content_for_different_patients_has_two_concurrent_winners(tmp_path):
    require_postgresql()
    first_user, first_patient = user_and_patient(
        email="first-concurrent@example.com",
        digital_id="60000000000000002",
    )
    second_user, second_patient = user_and_patient(
        email="second-concurrent@example.com",
        digital_id="60000000000000003",
    )

    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        results, failures = run_concurrently(
            operation(first_user.uuid, first_patient.uuid),
            operation(second_user.uuid, second_patient.uuid),
        )

    assert len(results) == 2
    assert not failures
    assert MedicalDocument.objects.count() == 2
    assert StoredFile.objects.count() == 2
    assert len([path for path in tmp_path.rglob("*") if path.is_file()]) == 2
