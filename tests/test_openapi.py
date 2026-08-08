def test_openapi_schema_contains_health_contract(api_client):
    response = api_client.get("/api/v1/schema/")

    assert response.status_code == 200
    assert "/api/v1/health/" in response.data["paths"]
