import pytest


@pytest.mark.django_db
def test_openapi_documents_exact_m1_auth_operations(api_client):
    response = api_client.get("/api/v1/schema/", HTTP_ACCEPT="application/json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    expected = {
        "/api/v1/auth/register/": {"post"},
        "/api/v1/auth/login/": {"post"},
        "/api/v1/auth/refresh/": {"post"},
        "/api/v1/auth/logout/": {"post"},
        "/api/v1/auth/me/": {"get"},
    }
    for path, operations in expected.items():
        assert set(paths[path]) == operations


@pytest.mark.django_db
def test_openapi_auth_operations_have_request_and_response_schemas(api_client):
    schema = api_client.get("/api/v1/schema/", HTTP_ACCEPT="application/json").json()

    for path, method in [
        ("/api/v1/auth/register/", "post"),
        ("/api/v1/auth/login/", "post"),
        ("/api/v1/auth/refresh/", "post"),
        ("/api/v1/auth/logout/", "post"),
    ]:
        operation = schema["paths"][path][method]
        assert operation["requestBody"]["content"]["application/json"]["schema"]
        assert any(
            response.get("content", {}).get("application/json", {}).get("schema")
            for response in operation["responses"].values()
        )

    me = schema["paths"]["/api/v1/auth/me/"]["get"]
    assert me["security"]
    assert me["responses"]["200"]["content"]["application/json"]["schema"]
