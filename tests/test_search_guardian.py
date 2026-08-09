from datetime import date

import pytest
from django.db import connection
from django.utils import timezone

from tests.archive_helpers import verified_document
from tests.test_minor_medical_documents_api import (
    minor,
    relationship,
    verified_guardian,
)

pytestmark = pytest.mark.django_db


def guardian_search(minor_patient):
    return f"/api/v1/minors/{minor_patient.uuid}/search/"


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL-only guardian keyword search tests")


def test_verified_father_and_mother_can_search_child(api_client):
    father = verified_guardian(
        email="m13-father@example.com", digital_id="10000000000000041"
    )
    mother = verified_guardian(
        email="m13-mother@example.com", digital_id="10000000000000042"
    )
    patient = minor(digital_id="30000000000000041")
    relationship(father, patient, kind="FATHER")
    relationship(mother, patient, kind="MOTHER")
    document = verified_document(patient, father, date(2026, 3, 14), title="CBC Child")

    api_client.force_authenticate(user=father)
    father_view = api_client.get(f"{guardian_search(patient)}?year=2026")
    api_client.force_authenticate(user=mother)
    mother_view = api_client.get(f"{guardian_search(patient)}?year=2026")

    for response in (father_view, mother_view):
        assert response.status_code == 200
        assert response.data["data"]["count"] == 1
        assert response.data["data"]["results"][0]["uuid"] == str(document.uuid)
        encoded = str(response.data)
        for forbidden in ("father", "mother", "@example.com", "digital_id"):
            assert forbidden not in encoded.lower()


@pytest.mark.parametrize(
    ("state", "kwargs"),
    [
        ("pending", {"status": "PENDING", "active": False}),
        ("rejected", {"status": "REJECTED", "active": False}),
        ("inactive", {"status": "VERIFIED", "active": False}),
    ],
)
def test_non_live_guardian_denied(api_client, state, kwargs):
    guardian = verified_guardian(
        email=f"m13-{state}@example.com",
        digital_id={
            "pending": "10000000000000043",
            "rejected": "10000000000000044",
            "inactive": "10000000000000045",
        }[state],
    )
    patient = minor(digital_id="30000000000000042")
    relationship(guardian, patient, **kwargs)
    api_client.force_authenticate(user=guardian)
    assert api_client.get(guardian_search(patient)).status_code == 404


def test_unrelated_guardian_denied(api_client):
    guardian = verified_guardian(
        email="m13-unrelated@example.com", digital_id="10000000000000046"
    )
    patient = minor(digital_id="30000000000000043")
    api_client.force_authenticate(user=guardian)
    assert api_client.get(guardian_search(patient)).status_code == 404


def test_exact_age_18_minor_search_denied(api_client):
    guardian = verified_guardian(
        email="m13-ageout@example.com", digital_id="10000000000000047"
    )
    today = timezone.localdate()
    adult_today = minor(
        digital_id="30000000000000044",
        date_of_birth=date(today.year - 18, today.month, today.day),
    )
    relationship(guardian, adult_today)
    api_client.force_authenticate(user=guardian)
    assert adult_today.is_minor is False
    assert api_client.get(guardian_search(adult_today)).status_code == 404


def test_child_remains_document_patient(api_client):
    father = verified_guardian(
        email="m13-owner@example.com", digital_id="10000000000000048"
    )
    patient = minor(digital_id="30000000000000045")
    relationship(father, patient, kind="FATHER")
    document = verified_document(patient, father, date(2026, 2, 1), title="Child")
    api_client.force_authenticate(user=father)
    response = api_client.get(guardian_search(patient))
    assert response.status_code == 200
    assert document.patient == patient
    assert response.data["data"]["results"][0]["uuid"] == str(document.uuid)


def test_guardian_keyword_search(api_client):
    require_postgresql()
    father = verified_guardian(
        email="m13-kw@example.com", digital_id="10000000000000049"
    )
    patient = minor(digital_id="30000000000000046")
    relationship(father, patient, kind="FATHER")
    target = verified_document(patient, father, date(2026, 3, 14), title="CBC Child")
    verified_document(patient, father, date(2026, 3, 15), title="XRay Child")
    api_client.force_authenticate(user=father)
    response = api_client.get(f"{guardian_search(patient)}?q=cbc")
    assert response.status_code == 200
    assert [r["uuid"] for r in response.data["data"]["results"]] == [str(target.uuid)]


def test_verification_agent_cannot_search_minor(api_client):
    from accounts.models import User

    agent = User.objects.create_user(
        email="m13-agent@example.com",
        password="A-complex-password-2026!",
        role=User.Role.IDENTITY_VERIFICATION_AGENT,
        status=User.Status.ACTIVE,
    )
    patient = minor(digital_id="30000000000000047")
    api_client.force_authenticate(user=agent)
    # No guardian relationship exists for the agent: normal 404 denial.
    assert api_client.get(guardian_search(patient)).status_code == 404
