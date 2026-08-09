import pytest
from django.db import connection

from tests.archive_helpers import attach_text, make_document, verified_document
from tests.test_medical_documents_api import patient_user

pytestmark = [pytest.mark.django_db, pytest.mark.postgresql]

SEARCH = "/api/v1/search/"


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL-only search keyword tests")


def authenticate(client, user):
    client.force_authenticate(user=user)


def search_uuids(api_client, url):
    response = api_client.get(url)
    assert response.status_code == 200
    return {row["uuid"] for row in response.data["data"]["results"]}


def test_keyword_matches_title(api_client):
    require_postgresql()
    user, patient = patient_user()
    target = verified_document(patient, user, "2026-03-14", title="CBC Result Annual")
    verified_document(patient, user, "2026-03-15", title="Blood Glucose")
    authenticate(api_client, user)
    assert search_uuids(api_client, f"{SEARCH}?q=cbc") == {str(target.uuid)}


def test_keyword_matches_description(api_client):
    require_postgresql()
    user, patient = patient_user()
    target = verified_document(
        patient, user, "2026-03-14", description="Hemoglobin level follow-up"
    )
    authenticate(api_client, user)
    assert search_uuids(api_client, f"{SEARCH}?q=hemoglobin") == {str(target.uuid)}


def test_keyword_matches_raw_facility_name(api_client):
    require_postgresql()
    user, patient = patient_user()
    target = verified_document(
        patient, user, "2026-03-14", facility_name="Baghdad Teaching Hospital"
    )
    authenticate(api_client, user)
    assert search_uuids(api_client, f"{SEARCH}?q=teaching") == {str(target.uuid)}


def test_keyword_matches_location(api_client):
    require_postgresql()
    user, patient = patient_user()
    target = verified_document(
        patient, user, "2026-03-14", location_text="Karkh Medical District"
    )
    authenticate(api_client, user)
    assert search_uuids(api_client, f"{SEARCH}?q=karkh") == {str(target.uuid)}


def test_keyword_matches_department(api_client):
    require_postgresql()
    user, patient = patient_user()
    target = verified_document(patient, user, "2026-03-14", department="Cardiology")
    authenticate(api_client, user)
    # q is whole-token lexical search: full token matches, substrings do not.
    assert search_uuids(api_client, f"{SEARCH}?q=cardiology") == {str(target.uuid)}


def test_keyword_matches_physician_name(api_client):
    require_postgresql()
    user, patient = patient_user()
    target = verified_document(
        patient, user, "2026-03-14", physician_name="Dr Ali Hassan"
    )
    authenticate(api_client, user)
    assert search_uuids(api_client, f"{SEARCH}?q=ali") == {str(target.uuid)}


def test_keyword_is_case_insensitive(api_client):
    require_postgresql()
    user, patient = patient_user()
    target = verified_document(patient, user, "2026-03-14", title="CBC Result Annual")
    authenticate(api_client, user)
    for term in ("cbc", "CBC", "CbC"):
        assert search_uuids(api_client, f"{SEARCH}?q={term}") == {str(target.uuid)}


def test_keyword_matches_canonical_extracted_text(api_client):
    require_postgresql()
    user, patient = patient_user()
    target = verified_document(patient, user, "2026-03-14", title="Lab Report")
    attach_text(target, "The hemoglobin level is within normal range.")
    other = verified_document(patient, user, "2026-03-15", title="Other Report")
    attach_text(other, "Creatinine clearance measured.")
    authenticate(api_client, user)
    assert search_uuids(api_client, f"{SEARCH}?q=hemoglobin") == {str(target.uuid)}
    # No snippet/content exposed in the response.
    encoded = str(api_client.get(f"{SEARCH}?q=hemoglobin").data)
    assert "normal range" not in encoded
    assert "hemoglobin" not in encoded


def test_metadata_only_document_without_text_is_searchable(api_client):
    require_postgresql()
    user, patient = patient_user()
    # No DocumentText at all: still found through metadata.
    target = verified_document(
        patient, user, "2026-03-14", title="CBC Result", department="Hematology"
    )
    authenticate(api_client, user)
    assert search_uuids(api_client, f"{SEARCH}?q=cbc") == {str(target.uuid)}
    assert search_uuids(api_client, f"{SEARCH}?q=hematology") == {str(target.uuid)}


def test_ocr_failed_but_metadata_searchable(api_client):
    require_postgresql()
    user, patient = patient_user()
    target = make_document(
        patient,
        user,
        processing_status="FAILED",
        document_date="2026-03-14",
        date_verified=True,
        date_source="USER_CORRECTED",
        title="CBC Result",
        facility_name="Central Lab",
    )
    authenticate(api_client, user)
    assert search_uuids(api_client, f"{SEARCH}?q=cbc") == {str(target.uuid)}


def test_keyword_and_structured_filter_combine(api_client):
    require_postgresql()
    user, patient = patient_user()
    target = verified_document(
        patient,
        user,
        "2026-03-14",
        title="CBC Result",
        document_type="LABORATORY",
    )
    verified_document(
        patient, user, "2026-03-15", title="CBC Radiology", document_type="RADIOLOGY"
    )
    authenticate(api_client, user)
    url = f"{SEARCH}?q=cbc&year=2026&document_type=LABORATORY"
    assert search_uuids(api_client, url) == {str(target.uuid)}


def test_keyword_in_unconfirmed_bucket(api_client):
    require_postgresql()
    user, patient = patient_user()
    target = make_document(patient, user, title="CBC Unconfirmed")
    authenticate(api_client, user)
    assert search_uuids(api_client, f"{SEARCH}?q=cbc&date_status=UNCONFIRMED") == {
        str(target.uuid)
    }
    assert search_uuids(api_client, f"{SEARCH}?q=cbc") == set()


def test_arabic_metadata_token_searchable(api_client):
    require_postgresql()
    user, patient = patient_user()
    target = verified_document(patient, user, "2026-03-14", title="تحليل دم شامل")
    authenticate(api_client, user)
    assert search_uuids(api_client, f"{SEARCH}?q=دم") == {str(target.uuid)}


def test_arabic_canonical_text_token_searchable(api_client):
    require_postgresql()
    user, patient = patient_user()
    target = verified_document(patient, user, "2026-03-14", title="تقرير مختبر")
    attach_text(target, "قيمة الهيموغلوبين ضمن المعدل الطبيعي")
    authenticate(api_client, user)
    assert search_uuids(api_client, f"{SEARCH}?q=الهيموغلوبين") == {str(target.uuid)}


def test_patient_isolation_with_same_keyword(api_client):
    require_postgresql()
    user, patient = patient_user()
    other_user, other_patient = patient_user(
        email="other-search@example.com", digital_id="76543210987654309"
    )
    verified_document(patient, user, "2026-03-14", title="CBC Result")
    verified_document(other_patient, other_user, "2026-03-14", title="CBC Result")
    authenticate(api_client, user)
    results = search_uuids(api_client, f"{SEARCH}?q=cbc")
    assert len(results) == 1
    assert results == {str(patient.medical_documents.get().uuid)}


def test_soft_deleted_document_never_in_keyword_search(api_client):
    require_postgresql()
    user, patient = patient_user()
    from documents.models import MedicalDocument

    deleted = verified_document(patient, user, "2026-03-14", title="CBC Deleted")
    attach_text(deleted, "hemoglobin strong match")
    deleted.archive_status = MedicalDocument.ArchiveStatus.DELETED
    deleted.save(update_fields=("archive_status", "updated_at"))
    authenticate(api_client, user)
    assert search_uuids(api_client, f"{SEARCH}?q=hemoglobin") == set()
    assert search_uuids(api_client, f"{SEARCH}?q=cbc") == set()
