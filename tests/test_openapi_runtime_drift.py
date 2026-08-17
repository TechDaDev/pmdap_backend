"""M17 runtime-vs-OpenAPI contract drift tests.

For representative endpoints the test performs a real API call and verifies the
actual JSON response matches the documented OpenAPI schema. This catches
contract drift where runtime returns fields the schema does not document, or
where the success/error envelope shape diverges from the frozen format.

Search is PostgreSQL-only (django.contrib.postgres search vectors); it is
guarded and self-skips on the SQLite test lane.
"""

from datetime import date

import pytest
from django.conf import settings
from django.db import connection

from processing.models import DateCandidate
from tests.archive_helpers import attach_text, make_facility, verified_document
from tests.test_identity_documents import (
    COLLECTION as IDENTITY_COLLECTION,
)
from tests.test_identity_documents import (
    create_patient,
)
from tests.test_identity_documents import (
    image_upload as identity_image_upload,
)
from tests.test_identity_documents import (
    national_card_payload as identity_national_card_payload,
)
from tests.test_identity_documents import (
    submit as identity_submit,
)
from tests.test_medical_documents_api import (
    COLLECTION as DOCUMENTS_COLLECTION,
)
from tests.test_medical_documents_api import (
    authenticate as documents_authenticate,
)
from tests.test_medical_documents_api import (
    patient_user,
)
from tests.test_medical_documents_api import (
    payload as document_payload,
)
from tests.test_minors_guardians import (
    MINORS,
    create_minor,
    create_verified_guardian,
)
from tests.test_minors_guardians import (
    birth_document_payload as minor_birth_payload,
)

pytestmark = pytest.mark.django_db


def resolve_schema(schema, node):
    if "allOf" in node:
        return resolve_schema(schema, node["allOf"][0])
    if "anyOf" in node:
        return resolve_schema(schema, node["anyOf"][0])
    if "$ref" in node:
        return schema["components"]["schemas"][node["$ref"].rsplit("/", 1)[-1]]
    return node


def data_schema(schema, path, method, status=200):
    """Resolve the documented schema for the envelope's ``data`` field."""
    operation = schema["paths"][path][method]
    response = operation["responses"].get(str(status))
    if response is None:
        return None
    content = response.get("content", {})
    media = content.get("application/json")
    if media is None:
        return None
    envelope = resolve_schema(schema, media.get("schema", {}))
    return resolve_schema(schema, envelope.get("properties", {}).get("data", {}))


def assert_runtime_matches_data_schema(schema, response, path, method, status=200):
    assert response.status_code == status, (path, method, response.content)
    body = response.json()
    assert set(body) == {"data"}, (path, method, "envelope shape drift", body)
    documented = data_schema(schema, path, method, status)
    assert documented is not None, (path, method, "no documented data schema")
    runtime_keys = set(body["data"])
    documented_keys = set(documented.get("properties", {}))
    unexpected = runtime_keys - documented_keys
    assert not unexpected, (
        path,
        method,
        "runtime returns fields the OpenAPI schema does not document",
        sorted(unexpected),
    )


def schema(api_client):
    response = api_client.get("/api/v1/schema/", HTTP_ACCEPT="application/json")
    assert response.status_code == 200
    return response.json()


def test_health_runtime_matches_schema(api_client):
    """Health is the deliberate non-enveloped liveness probe."""
    s = schema(api_client)
    response = api_client.get("/api/v1/health/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    documented = resolve_schema(
        s,
        s["paths"]["/api/v1/health/"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"],
    )
    assert set(documented.get("properties", {})) == {"status"}


def test_register_login_me_runtime_match_schema(api_client):
    s = schema(api_client)
    email = "drift-user@example.com"
    register = api_client.post(
        "/api/v1/auth/register/",
        {
            "email": email,
            "password": "A-complex-password-2026!",
            "phone": "+9647000000000",
            "patient": {
                "full_name": "Drift Patient",
                "date_of_birth": "1990-05-05",
                "sex": "UNSPECIFIED",
                "nationality": "IQ",
                "blood_group": "O+",
            },
        },
        format="json",
    )
    assert_runtime_matches_data_schema(
        s, register, "/api/v1/auth/register/", "post", 201
    )

    login = api_client.post(
        "/api/v1/auth/login/",
        {"email": email, "password": "A-complex-password-2026!"},
        format="json",
    )
    assert_runtime_matches_data_schema(s, login, "/api/v1/auth/login/", "post", 200)
    token = login.json()["data"]["access"]
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    me = api_client.get("/api/v1/auth/me/")
    assert_runtime_matches_data_schema(s, me, "/api/v1/auth/me/", "get", 200)


def test_patient_profile_runtime_matches_schema(api_client):
    s = schema(api_client)
    user, _ = patient_user()
    documents_authenticate(api_client, user)
    response = api_client.get("/api/v1/patients/me/")
    assert_runtime_matches_data_schema(s, response, "/api/v1/patients/me/", "get", 200)


def test_identity_submit_and_detail_runtime_match_schema(
    api_client, settings, tmp_path
):
    settings.IDENTITY_FILE_ROOT = tmp_path / "private-identity"
    s = schema(api_client)
    user, _ = create_patient()
    submitted = identity_submit(api_client, user, identity_national_card_payload())
    assert_runtime_matches_data_schema(s, submitted, IDENTITY_COLLECTION, "post", 201)
    document_uuid = submitted.json()["data"]["uuid"]
    documents_authenticate(api_client, user)
    detail = api_client.get(f"{IDENTITY_COLLECTION}{document_uuid}/")
    assert_runtime_matches_data_schema(
        s, detail, f"{IDENTITY_COLLECTION}{{document_uuid}}/", "get", 200
    )


def test_minor_creation_runtime_matches_schema(api_client, settings, tmp_path):
    settings.IDENTITY_FILE_ROOT = tmp_path / "private-identity"
    s = schema(api_client)
    guardian, _, _ = create_verified_guardian()
    created = create_minor(api_client, guardian, minor_birth_payload())
    assert_runtime_matches_data_schema(s, created, MINORS, "post", 201)


def _upload_document(api_client, user, patient):
    documents_authenticate(api_client, user)
    response = api_client.post(
        DOCUMENTS_COLLECTION, document_payload(), format="multipart"
    )
    assert response.status_code == 201, response.content
    return response


def test_medical_upload_detail_candidates_confirm_runtime_match_schema(
    api_client, tmp_path
):
    from django.test import override_settings

    s = schema(api_client)
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        user, patient = patient_user()
        created = _upload_document(api_client, user, patient)
        assert_runtime_matches_data_schema(
            s, created, DOCUMENTS_COLLECTION, "post", 201
        )
        document_uuid = created.json()["data"]["uuid"]

        DateCandidate.objects.create(
            document_id=document_uuid,
            detected_date=date(2026, 3, 14),
            candidate_type="REPORT_DATE",
            score=0.9,
            page_number=1,
            occurrence_index=0,
            context="Report Date",
            source="OCR",
            is_suggested=True,
            pipeline_version=settings.DATE_PIPELINE_VERSION,
        )

        detail = api_client.get(f"{DOCUMENTS_COLLECTION}{document_uuid}/")
        assert_runtime_matches_data_schema(
            s, detail, f"{DOCUMENTS_COLLECTION}{{document_uuid}}/", "get", 200
        )

        candidates = api_client.get(
            f"{DOCUMENTS_COLLECTION}{document_uuid}/date-candidates/"
        )
        assert_runtime_matches_data_schema(
            s,
            candidates,
            f"{DOCUMENTS_COLLECTION}{{document_uuid}}/date-candidates/",
            "get",
            200,
        )

        from documents.models import MedicalDocument

        MedicalDocument.objects.filter(pk=document_uuid).update(
            processing_status=MedicalDocument.ProcessingStatus.DATE_DETECTED
        )
        confirmed = api_client.post(
            f"{DOCUMENTS_COLLECTION}{document_uuid}/confirm-date/",
            {"date": "2026-03-14"},
            format="json",
        )
        assert_runtime_matches_data_schema(
            s,
            confirmed,
            f"{DOCUMENTS_COLLECTION}{{document_uuid}}/confirm-date/",
            "post",
            200,
        )


def test_facilities_list_and_detail_runtime_match_schema(api_client):
    s = schema(api_client)
    facility = make_facility()
    user, _ = patient_user()
    documents_authenticate(api_client, user)
    listing = api_client.get("/api/v1/facilities/")
    assert_runtime_matches_data_schema(s, listing, "/api/v1/facilities/", "get", 200)
    detail = api_client.get(f"/api/v1/facilities/{facility.uuid}/")
    assert_runtime_matches_data_schema(
        s, detail, "/api/v1/facilities/{facility_uuid}/", "get", 200
    )


def test_archive_and_summary_runtime_match_schema(api_client):
    s = schema(api_client)
    user, patient = patient_user()
    verified_document(patient, user, date(2026, 3, 14), title="Drift Report")
    documents_authenticate(api_client, user)
    listing = api_client.get("/api/v1/archive/")
    assert_runtime_matches_data_schema(s, listing, "/api/v1/archive/", "get", 200)
    summary = api_client.get("/api/v1/archive/summary/")
    assert_runtime_matches_data_schema(
        s, summary, "/api/v1/archive/summary/", "get", 200
    )


def test_search_runtime_matches_schema(api_client):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL-only search runtime test")
    s = schema(api_client)
    user, patient = patient_user()
    document = verified_document(
        patient, user, date(2026, 3, 14), title="CBC Result Annual"
    )
    attach_text(document, "haemoglobin 13.2 report")
    documents_authenticate(api_client, user)
    response = api_client.get("/api/v1/search/?q=haemoglobin")
    assert_runtime_matches_data_schema(s, response, "/api/v1/search/", "get", 200)


def test_error_envelope_shape_is_stable_across_domains(api_client):
    s = schema(api_client)
    # Unauthenticated access to a protected endpoint -> 401 documented envelope.
    unauthorized = api_client.get("/api/v1/documents/")
    assert unauthorized.status_code == 401
    error = unauthorized.json()["error"]
    assert set(error) == {"code", "message", "details"}
    assert error["code"] == "not_authenticated"
    documented = s["paths"]["/api/v1/documents/"]["get"]["responses"]["401"]
    assert "application/json" in documented["content"]

    # Validation failure -> 400 documented envelope.
    user, _ = patient_user()
    documents_authenticate(api_client, user)
    invalid = api_client.post(
        DOCUMENTS_COLLECTION,
        {"file": identity_image_upload("x.png"), "document_type": "NOT_A_TYPE"},
        format="multipart",
    )
    assert invalid.status_code == 400
    error = invalid.json()["error"]
    assert set(error) == {"code", "message", "details"}
    assert error["code"] == "validation_error"
    documented_400 = s["paths"][DOCUMENTS_COLLECTION]["post"]["responses"]["400"]
    assert "application/json" in documented_400["content"]
