from unittest.mock import patch

import pytest
from django.test import override_settings

from accounts.models import User
from documents.models import MedicalDocument, MedicalDocumentEvent
from facilities.models import AdministrativeRegion, City, Country
from facilities.services import (
    create_healthcare_facility,
    deactivate_healthcare_facility,
    update_healthcare_facility,
)
from tests.test_medical_documents_api import COLLECTION, patient_user, payload
from tests.test_minor_medical_documents_api import (
    collection as minor_collection,
)
from tests.test_minor_medical_documents_api import (
    minor,
    relationship,
    verified_guardian,
)
from tests.test_minor_medical_documents_api import (
    payload as minor_payload,
)

pytestmark = pytest.mark.django_db


def make_facility(*, active=True, name="Synthetic Medical Center"):
    country, _ = Country.objects.get_or_create(code="IQ", defaults={"name": "Iraq"})
    region, _ = AdministrativeRegion.objects.get_or_create(
        country=country,
        normalized_name="baghdad",
        defaults={"name": "Baghdad"},
    )
    city, _ = City.objects.get_or_create(
        region=region,
        normalized_name="baghdad",
        defaults={"name": "Baghdad"},
    )
    return create_healthcare_facility(
        name=name,
        country=country,
        region=region,
        city=city,
        facility_type="SPECIALIZED_CENTER",
        active=active,
    )


def create_document(api_client, tmp_path, **metadata):
    user, patient = patient_user()
    api_client.force_authenticate(user=user)
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        response = api_client.post(
            COLLECTION,
            payload(**metadata),
            format="multipart",
        )
    assert response.status_code == 201
    return (
        user,
        patient,
        MedicalDocument.objects.get(uuid=response.data["data"]["uuid"]),
    )


def test_owner_assigns_normalized_facility_and_preserves_raw_metadata(
    api_client, tmp_path
):
    facility = make_facility()
    _, _, document = create_document(
        api_client,
        tmp_path,
        facility_name="  Source Hosp.  ",
        document_date="2026-08-01",
    )
    original_file_id = document.stored_file_id
    original_date = document.document_date
    response = api_client.patch(
        f"{COLLECTION}{document.uuid}/",
        {
            "healthcare_facility_id": str(facility.uuid),
            "facility_name": "Source Hospital Raw",
            "location_text": "Baghdad / Karkh",
            "department": "  Cardiology  ",
            "physician_name": "  Dr Synthetic  ",
        },
        format="json",
    )
    document.refresh_from_db()
    assert response.status_code == 200
    assert document.healthcare_facility_id == facility.uuid
    assert response.data["data"]["healthcare_facility"]["uuid"] == str(facility.uuid)
    assert document.facility_name == "Source Hospital Raw"
    assert document.department == "Cardiology"
    assert document.physician_name == "Dr Synthetic"
    assert document.document_date == original_date
    assert document.stored_file_id == original_file_id
    event_types = set(document.events.values_list("event_type", flat=True))
    assert {
        MedicalDocumentEvent.EventType.DOCUMENT_FACILITY_CHANGED,
        MedicalDocumentEvent.EventType.DOCUMENT_LOCATION_UPDATED,
        MedicalDocumentEvent.EventType.DOCUMENT_DEPARTMENT_UPDATED,
        MedicalDocumentEvent.EventType.DOCUMENT_PHYSICIAN_METADATA_UPDATED,
    }.issubset(event_types)
    for event in document.events.exclude(
        event_type=MedicalDocumentEvent.EventType.UPLOADED
    ):
        assert "Source" not in str(event.metadata)
        assert "Synthetic" not in str(event.metadata)


@pytest.mark.parametrize("document_type", MedicalDocument.DocumentType.values)
def test_every_controlled_document_type_is_user_selected(
    api_client, tmp_path, document_type
):
    _, _, document = create_document(
        api_client,
        tmp_path,
        document_type=document_type,
    )
    assert document.document_type == document_type
    assert (
        document.classification_source
        == MedicalDocument.ClassificationSource.USER_SELECTED
    )


@pytest.mark.parametrize(
    ("initial", "updated"),
    [("OTHER", "LABORATORY"), ("RADIOLOGY", "MEDICAL_REPORT")],
)
def test_explicit_classification_transition_is_metadata_only(
    api_client, tmp_path, initial, updated
):
    _, _, document = create_document(api_client, tmp_path, document_type=initial)
    original_file_id = document.stored_file_id
    original_status = document.processing_status
    with (
        patch("processing.tasks.extract_pdf_text.delay") as pdf_task,
        patch("processing.tasks.ocr_medical_document.delay") as ocr_task,
        patch("processing.tasks.detect_document_dates.delay") as date_task,
    ):
        response = api_client.patch(
            f"{COLLECTION}{document.uuid}/",
            {"document_type": updated},
            format="json",
        )
    document.refresh_from_db()
    assert response.status_code == 200
    assert document.document_type == updated
    assert document.stored_file_id == original_file_id
    assert document.processing_status == original_status
    assert document.events.filter(
        event_type=MedicalDocumentEvent.EventType.DOCUMENT_TYPE_CHANGED,
        metadata__old_type=initial,
        metadata__new_type=updated,
    ).exists()
    pdf_task.assert_not_called()
    ocr_task.assert_not_called()
    date_task.assert_not_called()


def test_invalid_type_and_all_date_authority_fields_are_rejected(api_client, tmp_path):
    _, _, document = create_document(api_client, tmp_path)
    url = f"{COLLECTION}{document.uuid}/"
    for payload_data in (
        {"document_type": "AI_GUESSED"},
        {"document_date": "2026-01-01"},
        {"date_source": "OCR"},
        {"date_verified": True},
        {"date_verified_at": "2026-01-01T00:00:00Z"},
        {"processing_status": "INDEXED"},
    ):
        response = api_client.patch(url, payload_data, format="json")
        assert response.status_code == 400
        assert response.data["error"]["code"] == "validation_error"


def test_missing_and_inactive_facility_assignment_errors(api_client, tmp_path):
    inactive = make_facility(active=False)
    _, _, document = create_document(api_client, tmp_path)
    url = f"{COLLECTION}{document.uuid}/"
    missing = api_client.patch(
        url,
        {"healthcare_facility_id": "00000000-0000-0000-0000-000000000001"},
        format="json",
    )
    inactive_response = api_client.patch(
        url,
        {"healthcare_facility_id": str(inactive.uuid)},
        format="json",
    )
    assert missing.status_code == 404
    assert missing.data["error"]["code"] == "healthcare_facility_not_found"
    assert inactive_response.status_code == 409
    assert inactive_response.data["error"]["code"] == "healthcare_facility_inactive"


def test_historical_link_survives_deactivation_and_rename(api_client, tmp_path):
    facility = make_facility()
    _, _, document = create_document(
        api_client,
        tmp_path,
        facility_name="Original source label",
        healthcare_facility_id=str(facility.uuid),
    )
    facility = update_healthcare_facility(
        facility=facility, name="Renamed Synthetic Center"
    )
    deactivate_healthcare_facility(facility=facility)
    detail = api_client.get(f"{COLLECTION}{document.uuid}/")
    document.refresh_from_db()
    assert detail.status_code == 200
    assert detail.data["data"]["healthcare_facility"]["uuid"] == str(facility.uuid)
    assert detail.data["data"]["healthcare_facility"]["name"] == facility.name
    assert document.facility_name == "Original source label"
    assert document.healthcare_facility_id == facility.uuid


def test_guardian_classification_and_facility_authority(api_client, tmp_path):
    guardian = verified_guardian(
        email="m11-guardian@example.com", digital_id="11999999999999999"
    )
    patient = minor(digital_id="31999999999999999")
    relationship(guardian, patient)
    facility = make_facility()
    api_client.force_authenticate(user=guardian)
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        created = api_client.post(
            minor_collection(patient), minor_payload(), format="multipart"
        )
        response = api_client.patch(
            f"{minor_collection(patient)}{created.data['data']['uuid']}/",
            {
                "document_type": "LABORATORY",
                "healthcare_facility_id": str(facility.uuid),
            },
            format="json",
        )
    document = patient.medical_documents.get()
    assert response.status_code == 200
    assert document.patient == patient
    assert document.healthcare_facility_id == facility.uuid
    assert (
        document.classification_source
        == MedicalDocument.ClassificationSource.GUARDIAN_SELECTED
    )


def test_identity_verification_agent_cannot_access_document_association(
    api_client, tmp_path
):
    _, _, document = create_document(api_client, tmp_path)
    agent = User.objects.create_user(
        email="m11-agent@example.com",
        password="A-complex-password-2026!",
        role=User.Role.IDENTITY_VERIFICATION_AGENT,
        status=User.Status.ACTIVE,
    )
    api_client.force_authenticate(user=agent)
    detail = api_client.get(f"{COLLECTION}{document.uuid}/")
    changed = api_client.patch(
        f"{COLLECTION}{document.uuid}/", {"document_type": "OTHER"}, format="json"
    )
    directory = api_client.get("/api/v1/facilities/")
    assert detail.status_code == 403
    assert changed.status_code == 403
    assert directory.status_code == 200
    assert "medical_documents" not in str(directory.data)


def test_soft_deleted_document_cannot_receive_m11_metadata(api_client, tmp_path):
    facility = make_facility()
    _, _, document = create_document(api_client, tmp_path)
    url = f"{COLLECTION}{document.uuid}/"
    assert api_client.delete(url).status_code == 204
    response = api_client.patch(
        url,
        {"healthcare_facility_id": str(facility.uuid)},
        format="json",
    )
    assert response.status_code == 404
