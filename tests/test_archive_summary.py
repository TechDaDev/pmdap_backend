from datetime import date

import pytest

from documents.models import MedicalDocument
from tests.archive_helpers import make_document, make_facility, verified_document
from tests.test_medical_documents_api import patient_user

pytestmark = pytest.mark.django_db

SUMMARY = "/api/v1/archive/summary/"


def authenticate(client, user):
    client.force_authenticate(user=user)


def test_summary_year_month_type_facility_and_unconfirmed_counts(api_client):
    user, patient = patient_user()
    facility_a = make_facility(name="Facility A")
    facility_b = make_facility(name="Facility B")
    verified_document(patient, user, date(2026, 3, 14), healthcare_facility=facility_a)
    verified_document(patient, user, date(2026, 3, 20), healthcare_facility=facility_a)
    verified_document(patient, user, date(2026, 4, 2), healthcare_facility=facility_b)
    verified_document(patient, user, date(2025, 12, 5))
    make_document(patient, user)
    make_document(patient, user, processing_status="DATE_NOT_FOUND")
    authenticate(api_client, user)

    response = api_client.get(SUMMARY)
    assert response.status_code == 200
    body = response.data["data"]

    years = {row["year"]: row for row in body["years"]}
    assert years[2026]["count"] == 3
    assert years[2025]["count"] == 1
    months_2026 = {row["month"]: row["count"] for row in years[2026]["months"]}
    assert months_2026 == {3: 2, 4: 1}

    types = {row["document_type"]: row["count"] for row in body["document_types"]}
    assert types["LABORATORY"] == 6

    facilities = {row["name"]: row["count"] for row in body["facilities"]}
    assert facilities == {"Facility A": 2, "Facility B": 1}

    assert body["unconfirmed_date_count"] == 2


def test_summary_excludes_soft_deleted_from_all_groupings(api_client):
    user, patient = patient_user()
    facility = make_facility()
    active = verified_document(
        patient, user, date(2026, 3, 14), healthcare_facility=facility
    )
    deleted = verified_document(
        patient, user, date(2026, 3, 15), healthcare_facility=facility
    )
    unconfirmed = make_document(patient, user)
    deleted.archive_status = MedicalDocument.ArchiveStatus.DELETED
    deleted.save(update_fields=("archive_status", "updated_at"))
    authenticate(api_client, user)

    response = api_client.get(SUMMARY)
    body = response.data["data"]
    assert body["years"][0]["year"] == 2026
    assert body["years"][0]["count"] == 1
    assert body["years"][0]["months"] == [{"month": 3, "count": 1}]
    # Type counts include the unconfirmed active document but exclude the
    # soft-deleted one.
    assert body["document_types"] == [{"document_type": "LABORATORY", "count": 2}]
    assert body["facilities"] == [
        {"uuid": str(facility.uuid), "name": facility.name, "count": 1}
    ]
    assert body["unconfirmed_date_count"] == 1
    assert active.uuid
    assert unconfirmed.uuid


def test_summary_counts_are_patient_isolated(api_client):
    user, patient = patient_user()
    other_user, other_patient = patient_user(
        email="other-sum@example.com", digital_id="76543210987654310"
    )
    verified_document(patient, user, date(2026, 3, 14))
    verified_document(other_patient, other_user, date(2026, 3, 15))
    make_document(other_patient, other_user)
    authenticate(api_client, user)

    response = api_client.get(SUMMARY)
    body = response.data["data"]
    assert body["years"] == [
        {"year": 2026, "count": 1, "months": [{"month": 3, "count": 1}]}
    ]
    assert body["document_types"] == [{"document_type": "LABORATORY", "count": 1}]
    assert body["facilities"] == []
    assert body["unconfirmed_date_count"] == 0


def test_summary_empty_archive(api_client):
    user, _ = patient_user()
    authenticate(api_client, user)
    response = api_client.get(SUMMARY)
    assert response.status_code == 200
    assert response.data["data"] == {
        "years": [],
        "document_types": [],
        "facilities": [],
        "unconfirmed_date_count": 0,
    }
