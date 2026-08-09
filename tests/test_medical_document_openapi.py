import pytest

pytestmark = pytest.mark.django_db


def resolve_schema(schema, node):
    if "allOf" in node:
        return resolve_schema(schema, node["allOf"][0])
    if "$ref" not in node:
        return node
    name = node["$ref"].rsplit("/", 1)[-1]
    return schema["components"]["schemas"][name]


def test_openapi_documents_all_adult_and_minor_operations(api_client):
    schema = api_client.get("/api/v1/schema/", HTTP_ACCEPT="application/json").json()
    expected = {
        "/api/v1/documents/": {"get", "post"},
        "/api/v1/documents/{document_uuid}/": {"get", "patch", "delete"},
        "/api/v1/documents/{document_uuid}/file/": {"get"},
        "/api/v1/minors/{minor_uuid}/documents/": {"get", "post"},
        "/api/v1/minors/{minor_uuid}/documents/{document_uuid}/": {
            "get",
            "patch",
            "delete",
        },
        "/api/v1/minors/{minor_uuid}/documents/{document_uuid}/file/": {"get"},
    }

    for path, methods in expected.items():
        assert methods.issubset(schema["paths"][path])

    for path in (
        "/api/v1/documents/{document_uuid}/file/",
        "/api/v1/minors/{minor_uuid}/documents/{document_uuid}/file/",
    ):
        assert {"200", "401", "403", "404", "409", "503"}.issubset(
            schema["paths"][path]["get"]["responses"]
        )


def test_openapi_upload_is_multipart_and_matches_writable_contract(api_client):
    schema = api_client.get("/api/v1/schema/", HTTP_ACCEPT="application/json").json()

    for path in (
        "/api/v1/documents/",
        "/api/v1/minors/{minor_uuid}/documents/",
    ):
        operation = schema["paths"][path]["post"]
        content = operation["requestBody"]["content"]
        assert "multipart/form-data" in content
        request_schema = resolve_schema(
            schema, content["multipart/form-data"]["schema"]
        )
        assert set(request_schema["properties"]) == {
            "file",
            "document_type",
            "title",
            "description",
            "document_date",
            "healthcare_facility_id",
            "facility_name",
            "location_text",
            "department",
            "physician_name",
        }
        assert set(request_schema["required"]) == {"file", "document_type"}
        assert {
            "201",
            "400",
            "401",
            "403",
            "404",
            "409",
            "429",
            "503",
        }.issubset(operation["responses"])


def test_openapi_response_and_patch_do_not_expose_internal_storage_fields(api_client):
    schema = api_client.get("/api/v1/schema/", HTTP_ACCEPT="application/json").json()
    forbidden = {
        "sha256",
        "content_sha256",
        "storage_key",
        "path",
        "patient",
        "uploaded_by",
        "deleted_by",
    }

    patch = schema["paths"]["/api/v1/documents/{document_uuid}/"]["patch"]
    assert "409" in patch["responses"]
    patch_content = patch["requestBody"]["content"]["application/json"]["schema"]
    patch_schema = resolve_schema(schema, patch_content)
    assert forbidden.isdisjoint(patch_schema["properties"])
    assert set(patch_schema["properties"]) == {
        "document_type",
        "title",
        "description",
        "healthcare_facility_id",
        "facility_name",
        "location_text",
        "department",
        "physician_name",
    }

    serialized_schema = schema["components"]["schemas"]["MedicalDocument"]
    file_schema = resolve_schema(
        schema,
        serialized_schema["properties"]["file"],
    )
    assert forbidden.isdisjoint(serialized_schema["properties"])
    assert forbidden.isdisjoint(file_schema["properties"])
