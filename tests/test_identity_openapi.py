IDENTITY_PATHS = {
    "/api/v1/identity-documents/": {"get", "post"},
    "/api/v1/identity-documents/{document_uuid}/": {"get"},
    "/api/v1/identity-documents/{document_uuid}/replace/": {"post"},
    "/api/v1/identity-documents/{document_uuid}/images/{side}/": {"get"},
    "/api/v1/verification/identity-documents/": {"get"},
    "/api/v1/verification/identity-documents/{document_uuid}/": {"get"},
    "/api/v1/verification/identity-documents/{document_uuid}/approve/": {"post"},
    "/api/v1/verification/identity-documents/{document_uuid}/reject/": {"post"},
}


def resolve_schema(schema, value):
    if "$ref" not in value:
        return value
    name = value["$ref"].rsplit("/", 1)[-1]
    return schema["components"]["schemas"][name]


def test_m3_openapi_has_exact_routes_auth_and_responses(api_client):
    response = api_client.get("/api/v1/schema/", HTTP_ACCEPT="application/json")

    assert response.status_code == 200
    schema = response.json()
    for path, expected_methods in IDENTITY_PATHS.items():
        assert set(schema["paths"][path]) == expected_methods
        for operation in schema["paths"][path].values():
            assert operation["security"] == [{"bearerAuth": []}]
            assert operation["responses"]
            assert "401" in operation["responses"]


def test_multipart_contract_documents_binary_files(api_client):
    schema = api_client.get("/api/v1/schema/", HTTP_ACCEPT="application/json").json()

    for path in (
        "/api/v1/identity-documents/",
        "/api/v1/identity-documents/{document_uuid}/replace/",
    ):
        content = schema["paths"][path]["post"]["requestBody"]["content"]
        assert "multipart/form-data" in content
        request_schema = resolve_schema(
            schema, content["multipart/form-data"]["schema"]
        )
        assert request_schema["properties"]["front_image"] == {
            "type": "string",
            "format": "binary",
        }
        assert request_schema["properties"]["back_image"]["format"] == "binary"


def test_openapi_input_and_output_hide_internal_fields(api_client):
    schema = api_client.get("/api/v1/schema/", HTTP_ACCEPT="application/json").json()
    forbidden_output = {
        "file",
        "path",
        "url",
        "storage_key",
        "sha256",
        "front_image",
        "back_image",
        "verified_by",
        "patient_id",
    }
    forbidden_input = {
        "patient",
        "verification_status",
        "verified_by",
        "verified_at",
        "rejection_reason",
        "status",
        "sha256",
    }

    output = schema["components"]["schemas"]["IdentityDocumentDetail"]
    request = schema["components"]["schemas"]["IdentityDocumentInputRequest"]

    assert forbidden_output.isdisjoint(output["properties"])
    assert forbidden_input.isdisjoint(request["properties"])
    assert {"front_image", "back_image"}.issubset(request["properties"])


def test_verification_actions_document_actual_request_schemas(api_client):
    schema = api_client.get("/api/v1/schema/", HTTP_ACCEPT="application/json").json()
    approve = schema["paths"][
        "/api/v1/verification/identity-documents/{document_uuid}/approve/"
    ]["post"]
    reject = schema["paths"][
        "/api/v1/verification/identity-documents/{document_uuid}/reject/"
    ]["post"]

    assert "requestBody" not in approve
    reject_request = resolve_schema(
        schema,
        reject["requestBody"]["content"]["application/json"]["schema"],
    )
    assert set(reject_request["properties"]) == {"rejection_reason"}
    assert reject_request["required"] == ["rejection_reason"]
    for operation in (approve, reject):
        assert {"200", "400", "401", "403", "404", "409"}.issubset(
            operation["responses"]
        )
