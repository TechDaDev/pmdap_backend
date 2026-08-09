import pytest

pytestmark = pytest.mark.django_db

ARCHIVE_PATHS = {
    "/api/v1/archive/": ("get", "archive_list"),
    "/api/v1/archive/summary/": ("get", "archive_summary"),
    "/api/v1/minors/{minor_uuid}/archive/": ("get", "minor_archive_list"),
    "/api/v1/minors/{minor_uuid}/archive/summary/": (
        "get",
        "minor_archive_summary",
    ),
}


def test_openapi_documents_archive_routes(api_client):
    schema = api_client.get("/api/v1/schema/?format=json").json()
    for path, (method, operation_id) in ARCHIVE_PATHS.items():
        assert path in schema["paths"]
        operation = schema["paths"][path][method]
        assert operation["operationId"] == operation_id
        assert "200" in operation["responses"]
        assert "401" in operation["responses"]


def test_openapi_archive_list_documents_query_parameters(api_client):
    schema = api_client.get("/api/v1/schema/?format=json").json()
    operation = schema["paths"]["/api/v1/archive/"]["get"]
    param_names = {param["name"] for param in operation["parameters"]}
    assert {
        "date_status",
        "year",
        "month",
        "document_type",
        "healthcare_facility",
        "page",
    }.issubset(param_names)
    year_param = next(
        param for param in operation["parameters"] if param["name"] == "year"
    )
    assert year_param["schema"]["type"] == "integer"
    assert year_param.get("required", False) is False


def test_openapi_archive_response_omits_internal_fields(api_client):
    schema = api_client.get("/api/v1/schema/?format=json").json()
    document_schema = schema["components"]["schemas"]["ArchiveDocument"]
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
    }
    encoded = str(schema)
    for forbidden in ("sha256", "storage_key", "document_text", "content_sha256"):
        assert forbidden not in encoded


def test_openapi_archive_summary_shape(api_client):
    schema = api_client.get("/api/v1/schema/?format=json").json()
    summary = schema["components"]["schemas"]["ArchiveSummary"]
    assert set(summary["properties"]) == {
        "years",
        "document_types",
        "facilities",
        "unconfirmed_date_count",
    }
    year = schema["components"]["schemas"]["ArchiveSummaryYear"]
    assert set(year["properties"]) == {"year", "count", "months"}
