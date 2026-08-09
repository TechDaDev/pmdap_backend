import pytest

pytestmark = pytest.mark.django_db


def test_openapi_documents_exact_date_candidate_contract(api_client):
    schema = api_client.get("/api/v1/schema/?format=json").json()
    adult_path = "/api/v1/documents/{document_uuid}/date-candidates/"
    minor_path = (
        "/api/v1/minors/{minor_uuid}/documents/{document_uuid}/date-candidates/"
    )

    assert set(schema["paths"][adult_path]) == {"get"}
    assert set(schema["paths"][minor_path]) == {"get"}
    adult = schema["paths"][adult_path]["get"]
    assert adult["operationId"] == "medical_document_date_candidate_list"
    assert "200" in adult["responses"]
    assert "401" in adult["responses"]
    assert "404" in adult["responses"]

    candidate_schema = schema["components"]["schemas"]["DateCandidate"]
    assert set(candidate_schema["properties"]) == {
        "uuid",
        "date",
        "alternative_date",
        "type",
        "score",
        "page_number",
        "context",
        "source",
        "ambiguous",
        "is_suggested",
    }


def test_openapi_documents_exact_date_confirmation_contract(api_client):
    schema = api_client.get("/api/v1/schema/?format=json").json()
    adult_path = "/api/v1/documents/{document_uuid}/confirm-date/"
    minor_path = "/api/v1/minors/{minor_uuid}/documents/{document_uuid}/confirm-date/"

    assert set(schema["paths"][adult_path]) == {"post"}
    assert set(schema["paths"][minor_path]) == {"post"}
    adult = schema["paths"][adult_path]["post"]
    assert adult["operationId"] == "medical_document_date_confirm"
    assert {"200", "400", "401", "403", "404", "409"}.issubset(adult["responses"])
    request = adult["requestBody"]["content"]["application/json"]["schema"]
    request_schema = schema["components"]["schemas"][request["$ref"].rsplit("/", 1)[-1]]
    assert set(request_schema["properties"]) == {"candidate_id", "date"}
    response_schema = schema["components"]["schemas"][
        "DocumentDateConfirmationResponse"
    ]
    assert set(response_schema["properties"]) == {
        "uuid",
        "document_date",
        "date_source",
        "date_verified",
        "date_verified_at",
        "processing_status",
    }
