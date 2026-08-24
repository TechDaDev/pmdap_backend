from datetime import date, timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from accounts.models import User
from documents.models import MedicalDocument
from tests.archive_helpers import make_document, make_facility, verified_document
from tests.test_medical_documents_api import patient_user

pytestmark = pytest.mark.django_db

ARCHIVE = "/api/v1/archive/"
SUMMARY = "/api/v1/archive/summary/"


def authenticate(client, user):
    client.force_authenticate(user=user)


def test_empty_archive_returns_valid_paginated_response(api_client):
    user, _ = patient_user()
    authenticate(api_client, user)
    response = api_client.get(ARCHIVE)
    assert response.status_code == 200
    assert response.data["data"]["count"] == 0
    assert response.data["data"]["results"] == []
    assert response.data["data"]["unconfirmed_date_count"] == 0


def test_own_verified_documents_in_chronological_order(api_client):
    user, patient = patient_user()
    older = verified_document(patient, user, date(2025, 3, 10), title="older")
    middle = verified_document(patient, user, date(2026, 1, 15), title="middle")
    newest = verified_document(patient, user, date(2026, 8, 1), title="newest")
    authenticate(api_client, user)
    response = api_client.get(ARCHIVE)
    assert response.status_code == 200
    results = response.data["data"]["results"]
    assert [r["uuid"] for r in results] == [
        str(newest.uuid),
        str(middle.uuid),
        str(older.uuid),
    ]
    encoded = str(response.data)
    for forbidden in ("sha256", "storage_key", "document_text", "content_sha256"):
        assert forbidden not in encoded


def test_same_date_tie_break_is_deterministic(api_client):
    user, patient = patient_user()
    fixed_created = date(2026, 1, 1)
    documents = [
        verified_document(
            patient,
            user,
            date(2026, 3, 14),
            title=f"tie-{index}",
            created_at=fixed_created,
        )
        for index in range(5)
    ]
    expected = sorted((str(d.uuid) for d in documents), reverse=True)
    authenticate(api_client, user)
    response = api_client.get(ARCHIVE)
    results = response.data["data"]["results"]
    assert [r["uuid"] for r in results] == expected


def test_soft_deleted_documents_are_excluded(api_client):
    user, patient = patient_user()
    active = verified_document(patient, user, date(2026, 5, 1), title="active")
    deleted = verified_document(patient, user, date(2026, 5, 2), title="deleted")
    deleted.archive_status = MedicalDocument.ArchiveStatus.DELETED
    deleted.save(update_fields=("archive_status", "updated_at"))
    authenticate(api_client, user)
    response = api_client.get(ARCHIVE)
    assert response.status_code == 200
    results = response.data["data"]["results"]
    assert [r["uuid"] for r in results] == [str(active.uuid)]


def test_another_patients_documents_are_excluded(api_client):
    user, patient = patient_user()
    other_user, other_patient = patient_user(
        email="other@example.com", digital_id="76543210987654321"
    )
    verified_document(patient, user, date(2026, 1, 1), title="own")
    verified_document(other_patient, other_user, date(2026, 1, 2), title="other")
    authenticate(api_client, user)
    response = api_client.get(ARCHIVE)
    assert response.data["data"]["count"] == 1
    assert response.data["data"]["results"][0]["title"] == "own"


def test_unconfirmed_documents_visible_in_default_archive_and_listable(
    api_client,
):
    user, patient = patient_user()
    verified = verified_document(patient, user, date(2026, 3, 14), title="verified")
    awaiting = make_document(
        patient, user, processing_status="AWAITING_CONFIRMATION", title="awaiting"
    )
    date_not_found = make_document(
        patient,
        user,
        processing_status="DATE_NOT_FOUND",
        title="not-found",
    )
    failed = make_document(patient, user, processing_status="FAILED", title="failed")
    authenticate(api_client, user)

    # Archive = every active document, INCLUDING date-unconfirmed ones. Only
    # AWAITING_CONFIRMATION counts toward the confirmation queue.
    default = api_client.get(ARCHIVE)
    assert default.data["data"]["count"] == 4
    uuids = {r["uuid"] for r in default.data["data"]["results"]}
    assert uuids == {
        str(verified.uuid),
        str(awaiting.uuid),
        str(date_not_found.uuid),
        str(failed.uuid),
    }
    assert default.data["data"]["unconfirmed_date_count"] == 1

    unconfirmed = api_client.get(f"{ARCHIVE}?date_status=UNCONFIRMED")
    assert unconfirmed.data["data"]["count"] == 1
    assert [r["uuid"] for r in unconfirmed.data["data"]["results"]] == [
        str(awaiting.uuid)
    ]


def test_unconfirmed_ordering_by_created_at_then_uuid(api_client):
    user, patient = patient_user()
    fixed = date(2026, 1, 1)
    docs = [
        make_document(
            patient,
            user,
            processing_status="AWAITING_CONFIRMATION",
            title=f"u-{index}",
            created_at=fixed,
        )
        for index in range(4)
    ]
    authenticate(api_client, user)
    response = api_client.get(f"{ARCHIVE}?date_status=UNCONFIRMED")
    expected = sorted((str(d.uuid) for d in docs), reverse=True)
    assert [r["uuid"] for r in response.data["data"]["results"]] == expected


def test_unconfirmed_list_supports_type_and_facility_filters(api_client):
    user, patient = patient_user()
    facility = make_facility()
    target = make_document(
        patient,
        user,
        processing_status="AWAITING_CONFIRMATION",
        document_type="RADIOLOGY",
        healthcare_facility=facility,
        title="unconfirmed-target",
    )
    make_document(
        patient,
        user,
        processing_status="AWAITING_CONFIRMATION",
        document_type="LABORATORY",
    )
    make_document(
        patient,
        user,
        processing_status="AWAITING_CONFIRMATION",
        document_type="RADIOLOGY",
        healthcare_facility=None,
    )
    authenticate(api_client, user)

    by_type = api_client.get(
        f"{ARCHIVE}?date_status=UNCONFIRMED&document_type=RADIOLOGY"
    )
    assert by_type.data["data"]["count"] == 2

    by_facility = api_client.get(
        f"{ARCHIVE}?date_status=UNCONFIRMED&healthcare_facility={facility.uuid}"
    )
    assert by_facility.data["data"]["count"] == 1
    assert by_facility.data["data"]["results"][0]["uuid"] == str(target.uuid)

    combined = api_client.get(
        f"{ARCHIVE}?date_status=UNCONFIRMED&document_type=RADIOLOGY"
        f"&healthcare_facility={facility.uuid}"
    )
    assert combined.data["data"]["count"] == 1
    assert combined.data["data"]["results"][0]["uuid"] == str(target.uuid)


def test_year_filter_uses_verified_document_date(api_client):
    user, patient = patient_user()
    in_2026 = verified_document(patient, user, date(2026, 6, 1), title="in-2026")
    verified_document(patient, user, date(2025, 12, 31), title="in-2025")
    # An unconfirmed document with a future upload year must not match year 2026.
    make_document(patient, user, title="upload-only")
    authenticate(api_client, user)
    response = api_client.get(f"{ARCHIVE}?year=2026")
    assert response.data["data"]["count"] == 1
    assert response.data["data"]["results"][0]["uuid"] == str(in_2026.uuid)


def test_year_and_month_filter(api_client):
    user, patient = patient_user()
    march = verified_document(patient, user, date(2026, 3, 14), title="march")
    verified_document(patient, user, date(2026, 4, 2), title="april")
    verified_document(patient, user, date(2025, 3, 1), title="other-year")
    authenticate(api_client, user)
    response = api_client.get(f"{ARCHIVE}?year=2026&month=3")
    assert response.data["data"]["count"] == 1
    assert response.data["data"]["results"][0]["uuid"] == str(march.uuid)


def test_document_type_filter(api_client):
    user, patient = patient_user()
    lab = verified_document(patient, user, date(2026, 1, 1), document_type="LABORATORY")
    verified_document(patient, user, date(2026, 1, 2), document_type="RADIOLOGY")
    authenticate(api_client, user)
    response = api_client.get(f"{ARCHIVE}?document_type=LABORATORY")
    assert response.data["data"]["count"] == 1
    assert response.data["data"]["results"][0]["uuid"] == str(lab.uuid)


def test_facility_filter_applies_to_authorized_archive_only(api_client):
    user, patient = patient_user()
    other_user, other_patient = patient_user(
        email="other2@example.com", digital_id="76543210987654320"
    )
    facility_a = make_facility(name="Facility A")
    facility_b = make_facility(name="Facility B")
    own_a = verified_document(
        patient,
        user,
        date(2026, 1, 1),
        healthcare_facility=facility_a,
        facility_name="Raw A",
        title="own-a",
    )
    verified_document(patient, user, date(2026, 1, 2), healthcare_facility=facility_b)
    # Another patient has documents at Facility A: must stay invisible.
    verified_document(
        other_patient,
        other_user,
        date(2026, 1, 3),
        healthcare_facility=facility_a,
    )
    authenticate(api_client, user)
    response = api_client.get(f"{ARCHIVE}?healthcare_facility={facility_a.uuid}")
    assert response.data["data"]["count"] == 1
    assert response.data["data"]["results"][0]["uuid"] == str(own_a.uuid)
    assert response.data["data"]["results"][0]["facility_name"] == "Raw A"
    assert response.data["data"]["results"][0]["healthcare_facility"]["uuid"] == str(
        facility_a.uuid
    )


def test_combined_filters(api_client):
    user, patient = patient_user()
    facility = make_facility()
    target = verified_document(
        patient,
        user,
        date(2026, 3, 20),
        document_type="LABORATORY",
        healthcare_facility=facility,
        title="target",
    )
    verified_document(
        patient,
        user,
        date(2026, 3, 21),
        document_type="RADIOLOGY",
        healthcare_facility=facility,
    )
    verified_document(patient, user, date(2026, 3, 22), document_type="LABORATORY")
    authenticate(api_client, user)
    url = (
        f"{ARCHIVE}?year=2026&month=3&document_type=LABORATORY"
        f"&healthcare_facility={facility.uuid}"
    )
    response = api_client.get(url)
    assert response.data["data"]["count"] == 1
    assert response.data["data"]["results"][0]["uuid"] == str(target.uuid)


def test_raw_facility_without_normalized_link_remains_archiveable(api_client):
    user, patient = patient_user()
    verified_document(
        patient,
        user,
        date(2026, 2, 1),
        healthcare_facility=None,
        facility_name="Raw Hospital",
        location_text="Baghdad / Karkh",
        title="raw-only",
    )
    authenticate(api_client, user)
    response = api_client.get(ARCHIVE)
    assert response.data["data"]["count"] == 1
    row = response.data["data"]["results"][0]
    assert row["healthcare_facility"] is None
    assert row["facility_name"] == "Raw Hospital"
    assert row["location_text"] == "Baghdad / Karkh"


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("?month=3", "Month filter requires a year."),
        ("?year=2026&month=0", "Ensure this value is greater than or equal to 1."),
        ("?year=2026&month=13", "Ensure this value is less than or equal to 12."),
        ("?year=abc", "A valid integer is required."),
        ("?year=1", "Ensure this value is greater than or equal to 1900."),
        ("?year=99999", "Ensure this value is less than or equal to 2100."),
        ("?document_type=AI_GUESSED", "not a valid choice"),
        (
            "?healthcare_facility=not-a-uuid",
            "Must be a valid UUID.",
        ),
        (
            "?date_status=UNCONFIRMED&year=2026",
            "cannot be combined with year or month",
        ),
        (
            "?date_status=UNCONFIRMED&month=3&year=2026",
            "cannot be combined with year or month",
        ),
        ("?date_status=WEIRD", "not a valid choice"),
        ("?digital_id=12345678901234567", "This field is not allowed."),
        ("?patient_id=abc", "This field is not allowed."),
    ],
)
def test_invalid_and_incompatible_filters_are_rejected(api_client, query, message):
    user, _ = patient_user()
    authenticate(api_client, user)
    response = api_client.get(f"{ARCHIVE}{query}")
    assert response.status_code == 400
    assert response.data["error"]["code"] == "validation_error"
    assert message in str(response.data["error"]["details"])


def test_verification_agent_cannot_access_archive(api_client):
    user, patient = patient_user()
    verified_document(patient, user, date(2026, 1, 1))
    agent = User.objects.create_user(
        email="agent@example.com",
        password="A-complex-password-2026!",
        role=User.Role.IDENTITY_VERIFICATION_AGENT,
        status=User.Status.ACTIVE,
    )
    authenticate(api_client, agent)
    assert api_client.get(ARCHIVE).status_code == 403
    assert api_client.get(SUMMARY).status_code == 403


def test_archive_is_read_only_and_never_duplicates_files(api_client):
    user, patient = patient_user()
    document = verified_document(
        patient,
        user,
        date(2026, 3, 14),
        document_type="OTHER",
        facility_name="Raw",
    )
    authenticate(api_client, user)
    original_sha = document.stored_file.sha256
    original_name = document.stored_file.file.name
    stored_count = document.stored_file.__class__.objects.count()
    doc_count = MedicalDocument.objects.count()
    response = api_client.get(ARCHIVE)
    summary = api_client.get(SUMMARY)
    document.refresh_from_db()
    assert response.status_code == 200
    assert summary.status_code == 200
    assert document.stored_file.__class__.objects.count() == stored_count
    assert MedicalDocument.objects.count() == doc_count
    assert document.document_date == date(2026, 3, 14)
    assert document.document_type == "OTHER"
    assert document.facility_name == "Raw"
    assert document.stored_file.sha256 == original_sha
    assert document.stored_file.file.name == original_name


def test_archive_list_has_no_n_plus_one(api_client):
    user, patient = patient_user()
    facilities = [make_facility(name=f"Facility {index}") for index in range(3)]
    for index in range(9):
        verified_document(
            patient,
            user,
            date(2026, 1, 1) + timedelta(days=index),
            healthcare_facility=facilities[index % 3],
        )
    authenticate(api_client, user)
    with CaptureQueriesContext(connection) as captured:
        response = api_client.get(ARCHIVE)
    assert response.status_code == 200
    assert response.data["data"]["count"] == 9
    assert len(captured) <= 8


def test_archive_item_exposes_stored_file_mime_type(api_client):
    # M26: archive cards render a PDF/Image source tag from the ACTUAL stored
    # file media type — the payload must carry file.mime_type, never inferred
    # from document_type or page_count.
    user, patient = patient_user()
    verified_document(patient, user, date(2026, 1, 1))
    authenticate(api_client, user)
    response = api_client.get(ARCHIVE)
    assert response.status_code == 200
    results = response.data["data"]["results"]
    assert len(results) == 1
    item = results[0]
    assert item["file"]["mime_type"] == "image/png"
    assert item["file"]["page_count"] == 1
    assert "original_filename" in item["file"]
