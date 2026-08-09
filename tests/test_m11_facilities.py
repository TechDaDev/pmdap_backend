import uuid

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from accounts.models import User
from facilities.exceptions import InvalidLocationHierarchy
from facilities.models import (
    AdministrativeRegion,
    City,
    Country,
    HealthcareFacility,
    HealthcareFacilityAlias,
)
from facilities.services import (
    create_healthcare_facility,
    deactivate_healthcare_facility,
    update_healthcare_facility,
)

pytestmark = pytest.mark.django_db


def authenticated_user(client):
    user = User.objects.create_user(
        email="facility-reader@example.com",
        password="A-complex-password-2026!",
        status=User.Status.ACTIVE,
    )
    client.force_authenticate(user=user)
    return user


def hierarchy(*, country_code="IQ", suffix=""):
    country, _ = Country.objects.get_or_create(
        code=country_code,
        defaults={"name": "Iraq" if country_code == "IQ" else "Test Foreign"},
    )
    region, _ = AdministrativeRegion.objects.get_or_create(
        country=country,
        normalized_name=f"baghdad{suffix.lower()}",
        defaults={"name": f"Baghdad{suffix}"},
    )
    city = City.objects.create(region=region, name=f"Baghdad City{suffix}")
    return country, region, city


def facility(*, suffix="", active=True, facility_type="HOSPITAL"):
    country, region, city = hierarchy(suffix=suffix)
    return create_healthcare_facility(
        name=f"Synthetic Teaching Hospital{suffix}",
        country=country,
        region=region,
        city=city,
        facility_type=facility_type,
        active=active,
    )


def test_iraq_reference_and_foreign_country_are_representable():
    assert Country.objects.get(code="IQ").name == "Iraq"
    assert Country.objects.get(code="IQ").regions.count() == 19
    country, region, city = hierarchy(country_code="JO", suffix=" Foreign")
    foreign = create_healthcare_facility(
        name="Synthetic Foreign Clinic",
        country=country,
        region=region,
        city=city,
        facility_type=HealthcareFacility.FacilityType.CLINIC,
    )
    assert foreign.country_id == "JO"
    assert isinstance(foreign.uuid, uuid.UUID)


def test_normalization_aliases_and_duplicate_policy():
    item = facility()
    alias = HealthcareFacilityAlias.objects.create(
        facility=item,
        name="  SYNTHETIC   Teaching Hospital ",
        language=" EN ",
    )
    assert item.normalized_name == "synthetic teaching hospital"
    assert alias.normalized_name == item.normalized_name
    assert alias.language == "en"
    with pytest.raises(ValidationError):
        HealthcareFacilityAlias.objects.create(
            facility=item,
            name="synthetic teaching hospital",
        )
    with pytest.raises(InvalidLocationHierarchy):
        create_healthcare_facility(
            name=" synthetic teaching hospital ",
            country=item.country,
            region=item.region,
            city=item.city,
            facility_type=HealthcareFacility.FacilityType.HOSPITAL,
        )


def test_invalid_country_region_city_hierarchy_is_rejected():
    iq, iq_region, _ = hierarchy(suffix=" IQ")
    foreign, foreign_region, foreign_city = hierarchy(country_code="TR", suffix=" TR")
    with pytest.raises(InvalidLocationHierarchy):
        create_healthcare_facility(
            name="Cross Country Hospital",
            country=iq,
            region=foreign_region,
            city=foreign_city,
            facility_type=HealthcareFacility.FacilityType.HOSPITAL,
        )
    with pytest.raises(InvalidLocationHierarchy):
        create_healthcare_facility(
            name="Cross Region Hospital",
            country=foreign,
            region=iq_region,
            city=foreign_city,
            facility_type=HealthcareFacility.FacilityType.HOSPITAL,
        )


def test_database_city_requires_region_constraint():
    item = facility()
    with pytest.raises(IntegrityError):
        HealthcareFacility.objects.filter(pk=item.pk).update(region=None)


def test_rename_and_deactivation_preserve_uuid():
    item = facility()
    original_uuid = item.uuid
    item = update_healthcare_facility(facility=item, name="Renamed Synthetic Hospital")
    item = deactivate_healthcare_facility(facility=item)
    assert item.uuid == original_uuid
    assert item.normalized_name == "renamed synthetic hospital"
    assert item.active is False


def test_directory_requires_auth_and_is_paginated_without_association_leaks(
    api_client,
):
    item = facility()
    unauthenticated = api_client.get("/api/v1/facilities/")
    authenticated_user(api_client)
    response = api_client.get("/api/v1/facilities/")
    detail = api_client.get(f"/api/v1/facilities/{item.uuid}/")
    assert unauthenticated.status_code == 401
    assert response.status_code == 200
    assert set(response.data["data"]) == {
        "count",
        "next",
        "previous",
        "results",
    }
    assert response.data["data"]["count"] == 1
    encoded = str(detail.data).lower()
    assert detail.status_code == 200
    assert "patient" not in encoded
    assert "document" not in encoded


def test_directory_stable_order_and_whitelisted_filters(api_client):
    authenticated_user(api_client)
    second = facility(suffix=" Z", facility_type="CLINIC")
    first = facility(suffix=" A")
    response = api_client.get(
        "/api/v1/facilities/",
        {"country": "IQ", "region": "  BAGHDAD A ", "type": "HOSPITAL"},
    )
    assert [row["uuid"] for row in response.data["data"]["results"]] == [
        str(first.uuid)
    ]
    unfiltered = api_client.get("/api/v1/facilities/")
    names = [row["name"] for row in unfiltered.data["data"]["results"]]
    assert names == sorted(names, key=str.casefold)
    assert str(second.uuid) in {
        row["uuid"] for row in unfiltered.data["data"]["results"]
    }


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"country": "IRQ"}, "country"),
        ({"type": "WARD"}, "type"),
        ({"active": "not-bool"}, "active"),
        ({"unknown": "x"}, "unknown"),
    ],
)
def test_directory_rejects_malformed_filters(api_client, params, field):
    authenticated_user(api_client)
    response = api_client.get("/api/v1/facilities/", params)
    assert response.status_code == 400
    assert field in response.data["error"]["details"]


def test_inactive_directory_behavior_and_malformed_uuid(api_client):
    item = facility(active=False)
    authenticated_user(api_client)
    default = api_client.get("/api/v1/facilities/")
    explicit = api_client.get("/api/v1/facilities/", {"active": "false"})
    detail = api_client.get(f"/api/v1/facilities/{item.uuid}/")
    malformed = api_client.get("/api/v1/facilities/not-a-uuid/")
    assert default.data["data"]["count"] == 0
    assert explicit.data["data"]["count"] == 1
    assert detail.status_code == 404
    assert malformed.status_code == 404


def test_facility_openapi_is_read_only_and_safe(api_client):
    schema = api_client.get("/api/v1/schema/", HTTP_ACCEPT="application/json").json()
    assert set(schema["paths"]["/api/v1/facilities/"]) == {"get"}
    assert set(schema["paths"]["/api/v1/facilities/{facility_uuid}/"]) == {"get"}
    facility_schema = schema["components"]["schemas"]["HealthcareFacility"]
    assert {"patients", "documents", "medical_documents"}.isdisjoint(
        facility_schema["properties"]
    )
