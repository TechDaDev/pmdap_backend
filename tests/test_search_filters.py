from datetime import date, timedelta

import pytest

from accounts.models import User
from documents.date_services import confirm_document_date
from documents.models import MedicalDocument
from documents.services import update_medical_document
from tests.archive_helpers import make_document, make_facility, verified_document
from tests.test_medical_documents_api import patient_user

pytestmark = pytest.mark.django_db

SEARCH = "/api/v1/search/"
ARCHIVE = "/api/v1/archive/"


def authenticate(client, user):
    client.force_authenticate(user=user)


def collect_all(client, url):
    results = []
    next_url = url
    while next_url:
        response = client.get(next_url)
        assert response.status_code == 200
        body = response.data["data"]
        results.extend(body["results"])
        next_url = body["next"]
    return results


def test_empty_search_returns_valid_paginated_response(api_client):
    user, _ = patient_user()
    authenticate(api_client, user)
    response = api_client.get(SEARCH)
    assert response.status_code == 200
    assert response.data["data"]["count"] == 0
    assert response.data["data"]["results"] == []


def test_default_search_is_verified_chronology(api_client):
    user, patient = patient_user()
    older = verified_document(patient, user, date(2025, 1, 1), title="older")
    newer = verified_document(patient, user, date(2026, 8, 1), title="newer")
    make_document(patient, user, title="unconfirmed")
    authenticate(api_client, user)
    response = api_client.get(SEARCH)
    assert [r["uuid"] for r in response.data["data"]["results"]] == [
        str(newer.uuid),
        str(older.uuid),
    ]


def test_date_range_filters_verified_report_date(api_client):
    user, patient = patient_user()
    march = verified_document(patient, user, date(2026, 3, 14), title="march")
    verified_document(patient, user, date(2026, 4, 2), title="april")
    authenticate(api_client, user)
    url = f"{SEARCH}?date_from=2026-03-01&date_to=2026-03-31"
    assert [r["uuid"] for r in api_client.get(url).data["data"]["results"]] == [
        str(march.uuid)
    ]
    only_from = api_client.get(f"{SEARCH}?date_from=2026-04-01")
    assert only_from.data["data"]["count"] == 1
    only_to = api_client.get(f"{SEARCH}?date_to=2026-03-31")
    assert only_to.data["data"]["count"] == 1


def test_year_and_month_filters(api_client):
    user, patient = patient_user()
    verified_document(patient, user, date(2026, 3, 14), title="march")
    verified_document(patient, user, date(2026, 4, 2), title="april")
    verified_document(patient, user, date(2025, 3, 1), title="other-year")
    authenticate(api_client, user)
    assert api_client.get(f"{SEARCH}?year=2026").data["data"]["count"] == 2
    assert api_client.get(f"{SEARCH}?year=2026&month=3").data["data"]["count"] == 1


def test_document_type_filter(api_client):
    user, patient = patient_user()
    verified_document(patient, user, date(2026, 1, 1), document_type="LABORATORY")
    verified_document(patient, user, date(2026, 1, 2), document_type="RADIOLOGY")
    authenticate(api_client, user)
    assert (
        api_client.get(f"{SEARCH}?document_type=LABORATORY").data["data"]["count"] == 1
    )


def test_healthcare_facility_filter_and_isolation(api_client):
    user, patient = patient_user()
    other_user, other_patient = patient_user(
        email="iso@example.com", digital_id="76543210987654308"
    )
    facility = make_facility(name="Facility A")
    verified_document(patient, user, date(2026, 1, 1), healthcare_facility=facility)
    verified_document(
        other_patient, other_user, date(2026, 1, 2), healthcare_facility=facility
    )
    authenticate(api_client, user)
    response = api_client.get(f"{SEARCH}?healthcare_facility={facility.uuid}")
    assert response.data["data"]["count"] == 1


def test_department_filter_is_case_insensitive(api_client):
    user, patient = patient_user()
    verified_document(patient, user, date(2026, 1, 1), department="Cardiology")
    authenticate(api_client, user)
    for value in ("cardiology", "Cardiology", "CARDIOLOGY"):
        assert api_client.get(f"{SEARCH}?department={value}").data["data"]["count"] == 1


def test_physician_name_filter(api_client):
    user, patient = patient_user()
    target = verified_document(
        patient, user, date(2026, 1, 1), physician_name="Dr Ali Hassan"
    )
    authenticate(api_client, user)
    response = api_client.get(f"{SEARCH}?physician_name=ali")
    assert response.data["data"]["count"] == 1
    assert response.data["data"]["results"][0]["uuid"] == str(target.uuid)


def test_upload_date_filters_use_created_at(api_client):
    user, patient = patient_user()
    january = verified_document(
        patient, user, date(2026, 1, 1), created_at=date(2026, 1, 10)
    )
    verified_document(patient, user, date(2026, 1, 2), created_at=date(2026, 6, 1))
    authenticate(api_client, user)
    response = api_client.get(
        f"{SEARCH}?uploaded_from=2026-01-01&uploaded_to=2026-01-31"
    )
    assert [r["uuid"] for r in response.data["data"]["results"]] == [str(january.uuid)]


def test_date_status_unconfirmed_bucket(api_client):
    user, patient = patient_user()
    verified_document(patient, user, date(2026, 1, 1), title="verified")
    awaiting = make_document(patient, user, processing_status="AWAITING_CONFIRMATION")
    authenticate(api_client, user)
    response = api_client.get(f"{SEARCH}?date_status=UNCONFIRMED")
    assert [r["uuid"] for r in response.data["data"]["results"]] == [str(awaiting.uuid)]


def test_unconfirmed_ordering_is_created_at_then_uuid(api_client):
    user, patient = patient_user()
    docs = [
        make_document(patient, user, title=f"u-{i}", created_at=date(2026, 1, 1))
        for i in range(4)
    ]
    authenticate(api_client, user)
    response = api_client.get(f"{SEARCH}?date_status=UNCONFIRMED")
    expected = sorted((str(d.uuid) for d in docs), reverse=True)
    assert [r["uuid"] for r in response.data["data"]["results"]] == expected


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("?month=3", "Month filter requires a year"),
        ("?year=2026&month=0", "greater than or equal to 1"),
        ("?year=2026&month=13", "less than or equal to 12"),
        ("?year=abc", "A valid integer is required"),
        ("?document_type=AI_GUESSED", "not a valid choice"),
        ("?healthcare_facility=not-a-uuid", "Must be a valid UUID"),
        ("?date_from=not-a-date", "Date has wrong format"),
        ("?date_from=2026-04-02&date_to=2026-03-01", "cannot be after"),
        ("?uploaded_from=2026-04-02&uploaded_to=2026-03-01", "cannot be after"),
        ("?date_status=UNCONFIRMED&year=2026", "cannot be combined"),
        ("?date_status=UNCONFIRMED&month=3&year=2026", "cannot be combined"),
        ("?date_status=UNCONFIRMED&date_from=2026-01-01", "cannot be combined"),
        ("?date_status=WEIRD", "not a valid choice"),
        ("?patient_id=abc", "This field is not allowed"),
        ("?digital_id=12345678901234567", "This field is not allowed"),
        ("?guardian_id=abc", "This field is not allowed"),
        ("?raw_sql=SELECT+1", "This field is not allowed"),
        ("?regex=.*", "This field is not allowed"),
        ("?include_deleted=true", "This field is not allowed"),
    ],
)
def test_invalid_and_incompatible_filters_rejected(api_client, query, message):
    user, _ = patient_user()
    authenticate(api_client, user)
    response = api_client.get(f"{SEARCH}{query}")
    assert response.status_code == 400
    assert response.data["error"]["code"] == "validation_error"
    assert message in str(response.data["error"]["details"])


def test_query_too_long_rejected(api_client):
    user, _ = patient_user()
    authenticate(api_client, user)
    response = api_client.get(f"{SEARCH}?q={'a' * 201}")
    assert response.status_code == 400
    assert response.data["error"]["code"] == "validation_error"


def test_soft_deleted_documents_excluded_from_search(api_client):
    user, patient = patient_user()
    active = verified_document(patient, user, date(2026, 5, 1), title="active")
    deleted = verified_document(patient, user, date(2026, 5, 2), title="deleted")
    deleted.archive_status = MedicalDocument.ArchiveStatus.DELETED
    deleted.save(update_fields=("archive_status", "updated_at"))
    authenticate(api_client, user)
    response = api_client.get(SEARCH)
    assert [r["uuid"] for r in response.data["data"]["results"]] == [str(active.uuid)]


def test_verification_agent_cannot_search(api_client):
    user, patient = patient_user()
    verified_document(patient, user, date(2026, 1, 1), title="CBC Result")
    agent = User.objects.create_user(
        email="search-agent@example.com",
        password="A-complex-password-2026!",
        role=User.Role.IDENTITY_VERIFICATION_AGENT,
        status=User.Status.ACTIVE,
    )
    authenticate(api_client, agent)
    assert api_client.get(SEARCH).status_code == 403
    assert api_client.get(f"{SEARCH}?q=cbc").status_code == 403


def test_another_patients_documents_are_excluded(api_client):
    user, patient = patient_user()
    other_user, other_patient = patient_user(
        email="other@example.com", digital_id="76543210987654307"
    )
    verified_document(patient, user, date(2026, 1, 1), title="own")
    verified_document(other_patient, other_user, date(2026, 1, 2), title="other")
    authenticate(api_client, user)
    assert api_client.get(SEARCH).data["data"]["count"] == 1


def test_pagination_stable_and_complete(api_client):
    user, patient = patient_user()
    fixed_created = date(2026, 1, 1)
    docs = [
        verified_document(
            patient,
            user,
            date(2026, 1, 1) + timedelta(days=i),
            created_at=fixed_created,
        )
        for i in range(45)
    ]
    expected = sorted(
        docs, key=lambda d: (d.document_date, d.created_at, d.uuid), reverse=True
    )
    authenticate(api_client, user)
    first = collect_all(api_client, SEARCH)
    second = collect_all(api_client, SEARCH)
    assert len(first) == 45
    assert len({r["uuid"] for r in first}) == 45
    assert [r["uuid"] for r in first] == [str(d.uuid) for d in expected]
    assert [r["uuid"] for r in second] == [r["uuid"] for r in first]


def test_search_and_archive_agree_on_overlapping_filters(api_client):
    user, patient = patient_user()
    facility = make_facility()
    for i, doc_type in enumerate(("LABORATORY", "RADIOLOGY")):
        verified_document(
            patient,
            user,
            date(2026, 3, 14 + i),
            document_type=doc_type,
            healthcare_facility=facility,
        )
    make_document(patient, user)
    authenticate(api_client, user)

    archive = api_client.get(f"{ARCHIVE}?year=2026&document_type=LABORATORY")
    search = api_client.get(f"{SEARCH}?year=2026&document_type=LABORATORY")
    assert archive.status_code == 200
    assert search.status_code == 200
    archive_uuids = {r["uuid"] for r in archive.data["data"]["results"]}
    search_uuids = {r["uuid"] for r in search.data["data"]["results"]}
    assert archive_uuids == search_uuids
    assert len(search_uuids) == 1


def test_m10_date_correction_reflects_in_search(api_client):
    user, patient = patient_user()
    document = verified_document(patient, user, date(2026, 3, 14), title="CBC")
    authenticate(api_client, user)
    assert api_client.get(f"{SEARCH}?year=2026&month=3").data["data"]["count"] == 1
    confirm_document_date(
        document=document,
        actor=user,
        manual_date=date(2026, 4, 2),
    )
    assert api_client.get(f"{SEARCH}?year=2026&month=3").data["data"]["count"] == 0
    assert api_client.get(f"{SEARCH}?year=2026&month=4").data["data"]["count"] == 1


def test_m11_classification_and_facility_reflect_in_search(api_client):
    user, patient = patient_user()
    facility_a = make_facility(name="Facility A")
    facility_b = make_facility(name="Facility B")
    document = verified_document(
        patient,
        user,
        date(2026, 3, 14),
        document_type="OTHER",
        healthcare_facility=facility_a,
        facility_name="Raw Original",
    )
    authenticate(api_client, user)
    assert api_client.get(f"{SEARCH}?document_type=OTHER").data["data"]["count"] == 1
    assert (
        api_client.get(f"{SEARCH}?document_type=LABORATORY").data["data"]["count"] == 0
    )
    update_medical_document(
        document=document,
        actor=user,
        metadata={
            "document_type": "LABORATORY",
            "healthcare_facility_id": str(facility_b.uuid),
        },
    )
    assert (
        api_client.get(f"{SEARCH}?document_type=LABORATORY").data["data"]["count"] == 1
    )
    assert api_client.get(f"{SEARCH}?document_type=OTHER").data["data"]["count"] == 0
    assert (
        api_client.get(f"{SEARCH}?healthcare_facility={facility_b.uuid}").data["data"][
            "count"
        ]
        == 1
    )
    document.refresh_from_db()
    assert document.facility_name == "Raw Original"


def test_combined_filters_and_semantics(api_client):
    user, patient = patient_user()
    facility = make_facility()
    target = verified_document(
        patient,
        user,
        date(2026, 3, 20),
        document_type="LABORATORY",
        healthcare_facility=facility,
        department="Hematology",
        physician_name="Dr Ali",
    )
    verified_document(
        patient,
        user,
        date(2026, 3, 21),
        document_type="RADIOLOGY",
        healthcare_facility=facility,
    )
    authenticate(api_client, user)
    url = (
        f"{SEARCH}?year=2026&month=3&document_type=LABORATORY"
        f"&healthcare_facility={facility.uuid}&department=hematology"
        f"&physician_name=ali"
    )
    assert [r["uuid"] for r in api_client.get(url).data["data"]["results"]] == [
        str(target.uuid)
    ]
