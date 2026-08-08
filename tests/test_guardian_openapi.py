M4_PATHS = {
    "/api/v1/minors/": {"get", "post"},
    "/api/v1/minors/{minor_uuid}/": {"get"},
    "/api/v1/verification/guardian-relationships/": {"get"},
    "/api/v1/verification/guardian-relationships/{relationship_uuid}/": {"get"},
    "/api/v1/verification/guardian-relationships/{relationship_uuid}/approve/": {
        "post"
    },
    "/api/v1/verification/guardian-relationships/{relationship_uuid}/reject/": {"post"},
    "/api/v1/verification/guardian-relationships/"
    "{relationship_uuid}/evidence/{evidence_uuid}/file/": {"get"},
}


def resolve_schema(schema, value):
    if "$ref" not in value:
        return value
    name = value["$ref"].rsplit("/", 1)[-1]
    return schema["components"]["schemas"][name]


def test_m4_openapi_has_exact_routes_auth_and_responses(api_client):
    schema = api_client.get("/api/v1/schema/", HTTP_ACCEPT="application/json").json()

    for path, expected_methods in M4_PATHS.items():
        assert set(schema["paths"][path]) == expected_methods
        for operation in schema["paths"][path].values():
            assert operation["security"] == [{"bearerAuth": []}]
            assert operation["responses"]
            assert "401" in operation["responses"]


def test_minor_create_schema_is_multipart_and_protects_authority_fields(api_client):
    schema = api_client.get("/api/v1/schema/", HTTP_ACCEPT="application/json").json()
    operation = schema["paths"]["/api/v1/minors/"]["post"]
    content = operation["requestBody"]["content"]
    request_schema = resolve_schema(schema, content["multipart/form-data"]["schema"])

    assert operation["parameters"][0]["name"] == "Idempotency-Key"
    assert operation["parameters"][0]["required"] is True
    assert request_schema["properties"]["front_image"]["format"] == "binary"
    assert request_schema["properties"]["evidence_file"]["format"] == "binary"
    assert {
        "user",
        "digital_id",
        "identity_status",
        "verification_status",
        "active",
        "verified_by",
        "family_number_result",
    }.isdisjoint(request_schema["properties"])


def test_relationship_schema_does_not_expose_accounts_or_storage(api_client):
    schema = api_client.get("/api/v1/schema/", HTTP_ACCEPT="application/json").json()
    relationship = schema["components"]["schemas"]["GuardianRelationshipVerification"]
    serialized = str(relationship).lower()

    assert "email" not in serialized
    assert "phone" not in serialized
    assert "sha256" not in serialized
    assert "storage" not in serialized
    assert "family_number" not in relationship["properties"]


def test_relationship_decisions_document_actual_contracts(api_client):
    schema = api_client.get("/api/v1/schema/", HTTP_ACCEPT="application/json").json()
    base = "/api/v1/verification/guardian-relationships/{relationship_uuid}"
    approve = schema["paths"][f"{base}/approve/"]["post"]
    reject = schema["paths"][f"{base}/reject/"]["post"]

    assert "requestBody" not in approve
    rejection = resolve_schema(
        schema,
        reject["requestBody"]["content"]["application/json"]["schema"],
    )
    assert set(rejection["properties"]) == {"rejection_reason"}
    assert rejection["required"] == ["rejection_reason"]
    assert {"200", "400", "401", "403", "404", "409"}.issubset(approve["responses"])
