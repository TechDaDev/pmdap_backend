import pytest

pytestmark = pytest.mark.django_db

SEARCH_PATHS = {
    "/api/v1/search/": ("get", "medical_search"),
    "/api/v1/minors/{minor_uuid}/search/": ("get", "minor_medical_search"),
}


def test_openapi_documents_search_routes(api_client):
    schema = api_client.get("/api/v1/schema/?format=json").json()
    for path, (method, operation_id) in SEARCH_PATHS.items():
        assert path in schema["paths"]
        operation = schema["paths"][path][method]
        assert operation["operationId"] == operation_id
        assert "200" in operation["responses"]
        assert "400" in operation["responses"]
        assert "401" in operation["responses"]
        assert "429" in operation["responses"]


def test_openapi_documents_search_query_parameters(api_client):
    schema = api_client.get("/api/v1/schema/?format=json").json()
    operation = schema["paths"]["/api/v1/search/"]["get"]
    param_names = {param["name"] for param in operation["parameters"]}
    assert {
        "q",
        "date_from",
        "date_to",
        "year",
        "month",
        "document_type",
        "healthcare_facility",
        "department",
        "physician_name",
        "uploaded_from",
        "uploaded_to",
        "date_status",
        "page",
    }.issubset(param_names)
    q_param = next(param for param in operation["parameters"] if param["name"] == "q")
    assert q_param["schema"]["type"] == "string"
    assert q_param["schema"]["maxLength"] == 200


def test_openapi_search_results_reuse_safe_document_summary(api_client):
    schema = api_client.get("/api/v1/schema/?format=json").json()
    results = schema["components"]["schemas"]["SearchResultsPage"]["properties"][
        "results"
    ]
    document_schema = schema["components"]["schemas"][
        results["items"]["$ref"].rsplit("/", 1)[-1]
    ]
    assert set(document_schema["properties"]) == {
        "uuid",
        "title",
        "document_type",
        "document_date",
        "date_verified",
        "date_source",
        "healthcare_facility",
        "facility_name",
        "location_text",
        "department",
        "physician_name",
        "processing_status",
        "created_at",
        "file",
    }
    encoded = str(schema)
    for forbidden in ("sha256", "storage_key", "document_text", "search_vector"):
        assert forbidden not in encoded
