"""Patient lab-results endpoint tests (security + contract).

Synthetic values only. Verifies ownership, IDOR isolation, role leakage,
serialization safety, and the response contract (no OCR body / geometry).
"""
from datetime import date
from decimal import Decimal

import pytest
from django.test import override_settings

from accounts.models import User
from documents.models import MedicalDocument, StoredFile
from labs.models import LabReportExtraction, LabResult
from patients.models import PatientProfile
from processing.models import DocumentText, DocumentTextPage, DocumentTextSpan

pytestmark = pytest.mark.django_db

LAB_PATH = "/api/v1/documents/{uuid}/lab-results/"
MINOR_LAB_PATH = "/api/v1/minors/{minor}/documents/{doc}/lab-results/"


def make_patient(*, email, digital_id):
    user = User.objects.create_user(
        email=email,
        password="A-complex-password-2026!",
        status=User.Status.ACTIVE,
    )
    profile = PatientProfile.objects.create(
        user=user,
        digital_id=digital_id,
        full_name="Synthetic Patient",
        date_of_birth=date(1990, 1, 2),
        sex=PatientProfile.Sex.UNSPECIFIED,
        nationality="IQ",
    )
    return user, profile


def make_document(patient, user, *, document_type="LABORATORY", status="TEXT_EXTRACTED"):
    stored = StoredFile.objects.create(
        file=f"medical/{patient.digital_id}.jpg",
        original_filename="report.jpg",
        mime_type="image/jpeg",
        size_bytes=123,
        sha256="c" * 64,
        page_count=1,
        integrity_status=StoredFile.IntegrityStatus.VALID,
        malware_scan_status=StoredFile.MalwareScanStatus.CLEAN,
    )
    document = MedicalDocument.objects.create(
        patient=patient,
        uploaded_by=user,
        stored_file=stored,
        content_sha256="d" * 64,
        document_type=document_type,
        processing_status=status,
    )
    DocumentText.objects.create(
        document=document,
        text="Synthetic body",
        page_count=1,
        character_count=13,
        meaningful_character_count=13,
        usable=True,
        usability_reason="ok",
        has_pages_requiring_ocr=False,
        extraction_method=DocumentText.ExtractionMethod.OCR,
        extractor_name="paddleocr",
        extractor_version="3.7.0",
        pipeline_version="m8-ocr-v1",
    )
    return document


def make_extraction(document, *, status="COMPLETED", rows=2):
    extraction = LabReportExtraction.objects.create(
        document=document,
        pipeline_version="lab-v1",
        status=status,
        result_count=rows if status == "COMPLETED" else 0,
        extraction_confidence=0.95 if status == "COMPLETED" else None,
    )
    if status == "COMPLETED":
        for index in range(rows):
            LabResult.objects.create(
                extraction=extraction,
                page_number=1,
                row_index=index,
                test_name_raw=f"SYNTHETIC_TEST_{index}",
                test_name_normalized=f"TEST_{index}",
                result_raw=f"{index + 1}.25",
                result_numeric=Decimal(f"{index + 1}.25"),
                unit_raw="mg/dL",
                reference_range_raw="0.7 - 1.18",
                reference_low=Decimal("0.7"),
                reference_high=Decimal("1.18"),
                flag_raw="H" if index == 0 else "",
                extraction_confidence=0.93,
            )
    return extraction


def authenticate(client, user):
    client.force_authenticate(user=user)


def test_owner_completed_extraction_returns_ordered_raw_results(api_client):
    user, profile = make_patient(email="owner@example.com", digital_id="1" * 17)
    document = make_document(profile, user)
    make_extraction(document, rows=3)

    authenticate(api_client, user)
    response = api_client.get(LAB_PATH.format(uuid=document.uuid))

    assert response.status_code == 200
    data = response.data["data"]
    assert str(data["document_uuid"]) == str(document.uuid)
    assert data["document_type"] == "LABORATORY"
    assert data["extraction_status"] == "COMPLETED"
    assert data["pipeline_version"] == "lab-v1"
    assert data["result_count"] == 3
    results = data["results"]
    assert [r["row_index"] for r in results] == [0, 1, 2]
    first = results[0]
    assert first["test_name_raw"] == "SYNTHETIC_TEST_0"
    assert first["result_raw"] == "1.25"
    assert first["result_numeric"] == "1.25"  # Decimal serialized as string
    assert first["unit_raw"] == "mg/dL"
    assert first["reference_range_raw"] == "0.7 - 1.18"
    assert first["reference_low"] == "0.7"
    assert first["reference_high"] == "1.18"
    assert first["flag_raw"] == "H"
    assert isinstance(first["extraction_confidence"], float)


def test_response_never_exposes_geometry_or_ocr_body(api_client):
    user, profile = make_patient(email="owner2@example.com", digital_id="2" * 17)
    document = make_document(profile, user)
    page = DocumentTextPage.objects.create(
        document_text=DocumentText.objects.get(document=document),
        page_number=1,
        text="Synthetic body",
        meaningful_character_count=13,
        effective_source=DocumentTextPage.EffectiveSource.OCR,
        ocr_completed=True,
    )
    span = DocumentTextSpan.objects.create(
        document_text_page=page,
        sequence=0,
        text="Synthetic TEST_0 1.25 mg/dL",
        confidence=0.9,
        x_min=0.0,
        y_min=0.0,
        x_max=0.5,
        y_max=0.2,
        page_width=100,
        page_height=100,
    )
    make_extraction(document, rows=1)
    LabResult.objects.filter(extraction__document=document).first().source_spans.add(
        span
    )

    authenticate(api_client, user)
    response = api_client.get(LAB_PATH.format(uuid=document.uuid))

    encoded = str(response.data)
    # OCR body and geometry must never leak.
    assert "Synthetic body" not in encoded
    assert "Synthetic TEST_0" not in encoded
    for forbidden in (
        "source_spans",
        "bbox",
        "x_min",
        "x_max",
        "y_min",
        "y_max",
        "ocr_text",
        "native_text",
        "page_width",
        "page_height",
        "storage",
        "sha256",
    ):
        assert forbidden not in encoded, forbidden


def test_other_patient_document_is_404(api_client):
    owner, owner_profile = make_patient(
        email="owner3@example.com", digital_id="3" * 17
    )
    other, _ = make_patient(email="other@example.com", digital_id="4" * 17)
    document = make_document(owner_profile, owner)
    make_extraction(document)

    authenticate(api_client, other)
    response = api_client.get(LAB_PATH.format(uuid=document.uuid))

    assert response.status_code == 404
    assert response.data["error"]["code"] == "medical_document_not_found"


def test_unauthenticated_is_401(api_client):
    response = api_client.get(
        LAB_PATH.format(uuid="00000000-0000-0000-0000-000000000000")
    )
    assert response.status_code in (401, 403)


def test_verification_agent_denied(api_client):
    agent = User.objects.create_user(
        email="verification-agent@example.com",
        password="A-complex-password-2026!",
        status=User.Status.ACTIVE,
        role=User.Role.IDENTITY_VERIFICATION_AGENT,
    )
    owner, owner_profile = make_patient(
        email="owner4@example.com", digital_id="5" * 17
    )
    document = make_document(owner_profile, owner)
    make_extraction(document)

    authenticate(api_client, agent)
    response = api_client.get(LAB_PATH.format(uuid=document.uuid))

    assert response.status_code == 403
    assert response.data["error"]["code"] == "patient_role_required"


def test_non_laboratory_is_not_applicable(api_client):
    user, profile = make_patient(email="owner5@example.com", digital_id="6" * 17)
    document = make_document(profile, user, document_type="RADIOLOGY")

    authenticate(api_client, user)
    response = api_client.get(LAB_PATH.format(uuid=document.uuid))

    assert response.status_code == 200
    data = response.data["data"]
    assert data["extraction_status"] == "NOT_APPLICABLE"
    assert data["results"] == []
    assert data["result_count"] == 0


def test_failed_extraction_returns_empty_and_status(api_client):
    user, profile = make_patient(email="owner6@example.com", digital_id="7" * 17)
    document = make_document(profile, user)
    make_extraction(document, status="FAILED")

    authenticate(api_client, user)
    response = api_client.get(LAB_PATH.format(uuid=document.uuid))

    assert response.status_code == 200
    data = response.data["data"]
    assert data["extraction_status"] == "FAILED"
    assert data["results"] == []
    assert data["result_count"] == 0


def test_zero_row_completed(api_client):
    user, profile = make_patient(email="owner7@example.com", digital_id="8" * 17)
    document = make_document(profile, user)
    make_extraction(document, status="COMPLETED", rows=0)

    authenticate(api_client, user)
    response = api_client.get(LAB_PATH.format(uuid=document.uuid))

    assert response.status_code == 200
    data = response.data["data"]
    assert data["extraction_status"] == "COMPLETED"
    assert data["result_count"] == 0
    assert data["results"] == []


def test_no_extraction_yet_is_queued(api_client):
    user, profile = make_patient(email="owner8@example.com", digital_id="9" * 17)
    document = make_document(profile, user)

    authenticate(api_client, user)
    response = api_client.get(LAB_PATH.format(uuid=document.uuid))

    assert response.status_code == 200
    assert response.data["data"]["extraction_status"] == "QUEUED"


def test_deleted_document_is_404(api_client):
    user, profile = make_patient(email="owner9@example.com", digital_id="10000000000000009")
    document = make_document(profile, user)
    make_extraction(document)
    document.archive_status = MedicalDocument.ArchiveStatus.DELETED
    document.save(update_fields=("archive_status", "updated_at"))

    authenticate(api_client, user)
    response = api_client.get(LAB_PATH.format(uuid=document.uuid))

    assert response.status_code == 404


def test_endpoint_query_count_bounded(api_client, django_assert_num_queries):
    user, profile = make_patient(email="owner10@example.com", digital_id="10000000000000010")
    document = make_document(profile, user)
    make_extraction(document, rows=5)

    authenticate(api_client, user)
    # owned profile + document + extraction + results = 4, no N+1
    with django_assert_num_queries(4):
        response = api_client.get(LAB_PATH.format(uuid=document.uuid))

    assert response.status_code == 200
    assert len(response.data["data"]["results"]) == 5


def test_minor_guardian_access_authorized(api_client):
    from tests.test_minor_medical_documents_api import (
        minor as make_minor,
        relationship as make_relationship,
        verified_guardian,
    )

    guardian = verified_guardian(
        email="guardian@example.com", digital_id="10000000000000011"
    )
    minor_patient = make_minor(digital_id="30000000000000011")
    make_relationship(guardian, minor_patient)
    document = make_document(minor_patient, guardian)
    make_extraction(document, rows=1)

    authenticate(api_client, guardian)
    response = api_client.get(
        MINOR_LAB_PATH.format(minor=minor_patient.uuid, doc=document.uuid)
    )

    assert response.status_code == 200
    assert response.data["data"]["extraction_status"] == "COMPLETED"
    assert len(response.data["data"]["results"]) == 1


def test_minor_unrelated_guardian_is_404(api_client):
    from tests.test_minor_medical_documents_api import (
        minor as make_minor,
        verified_guardian,
    )

    unrelated = verified_guardian(
        email="unrelated@example.com", digital_id="10000000000000012"
    )
    minor_patient = make_minor(digital_id="30000000000000012")
    document = make_document(minor_patient, unrelated)
    make_extraction(document)

    authenticate(api_client, unrelated)
    response = api_client.get(
        MINOR_LAB_PATH.format(minor=minor_patient.uuid, doc=document.uuid)
    )

    assert response.status_code == 404
