from datetime import date

import pytest
from django.utils import timezone

from tests.archive_helpers import make_document, verified_document
from tests.test_minor_medical_documents_api import (
    minor,
    relationship,
    verified_guardian,
)

pytestmark = pytest.mark.django_db


def guardian_archive(minor_patient):
    return f"/api/v1/minors/{minor_patient.uuid}/archive/"


def guardian_summary(minor_patient):
    return f"/api/v1/minors/{minor_patient.uuid}/archive/summary/"


def test_verified_father_and_mother_each_see_child_archive(api_client):
    father = verified_guardian(
        email="m12-father@example.com", digital_id="10000000000000021"
    )
    mother = verified_guardian(
        email="m12-mother@example.com", digital_id="10000000000000022"
    )
    patient = minor(digital_id="30000000000000021")
    relationship(father, patient, kind="FATHER")
    relationship(mother, patient, kind="MOTHER")
    document = verified_document(
        patient, father, date(2026, 3, 14), title="child-report"
    )

    api_client.force_authenticate(user=father)
    father_list = api_client.get(guardian_archive(patient))
    father_summary = api_client.get(guardian_summary(patient))

    api_client.force_authenticate(user=mother)
    mother_list = api_client.get(guardian_archive(patient))
    mother_summary = api_client.get(guardian_summary(patient))

    for response in (father_list, mother_list):
        assert response.status_code == 200
        assert response.data["data"]["count"] == 1
        assert response.data["data"]["results"][0]["uuid"] == str(document.uuid)
    for response in (father_summary, mother_summary):
        assert response.status_code == 200
        assert response.data["data"]["years"][0]["count"] == 1
        # No guardian-account data is exposed.
        encoded = str(response.data)
        for forbidden in ("father", "mother", "guardian", "@example.com"):
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
        email=f"m12-{state}@example.com",
        digital_id={
            "pending": "10000000000000023",
            "rejected": "10000000000000024",
            "inactive": "10000000000000025",
        }[state],
    )
    patient = minor(digital_id="30000000000000022")
    relationship(guardian, patient, **kwargs)
    api_client.force_authenticate(user=guardian)
    assert api_client.get(guardian_archive(patient)).status_code == 404
    assert api_client.get(guardian_summary(patient)).status_code == 404


def test_unrelated_guardian_denied(api_client):
    guardian = verified_guardian(
        email="m12-unrelated@example.com", digital_id="10000000000000026"
    )
    patient = minor(digital_id="30000000000000023")
    api_client.force_authenticate(user=guardian)
    assert api_client.get(guardian_archive(patient)).status_code == 404
    assert api_client.get(guardian_summary(patient)).status_code == 404


def test_exact_age_18_minor_archive_denied(api_client):
    guardian = verified_guardian(
        email="m12-ageout@example.com", digital_id="10000000000000027"
    )
    today = timezone.localdate()
    adult_today = minor(
        digital_id="30000000000000024",
        date_of_birth=date(today.year - 18, today.month, today.day),
    )
    relationship(guardian, adult_today)
    api_client.force_authenticate(user=guardian)
    assert adult_today.is_minor is False
    assert api_client.get(guardian_archive(adult_today)).status_code == 404
    assert api_client.get(guardian_summary(adult_today)).status_code == 404


def test_child_remains_document_patient_and_no_guardian_data(api_client):
    father = verified_guardian(
        email="m12-owner@example.com", digital_id="10000000000000028"
    )
    patient = minor(digital_id="30000000000000025")
    relationship(father, patient, kind="FATHER")
    document = verified_document(patient, father, date(2026, 2, 1))
    make_document(patient, father)
    api_client.force_authenticate(user=father)

    response = api_client.get(guardian_archive(patient))
    assert response.status_code == 200
    assert document.patient == patient
    row = response.data["data"]["results"][0]
    encoded = str(response.data)
    assert "uploaded_by" not in encoded
    assert "digital_id" not in encoded
    assert "full_name" not in encoded
    assert "date_of_birth" not in encoded
    assert "identity_status" not in encoded
    assert row["uuid"] == str(document.uuid)


def test_two_guardians_do_not_learn_about_each_other(api_client):
    father = verified_guardian(
        email="m12-ind1@example.com", digital_id="10000000000000029"
    )
    mother = verified_guardian(
        email="m12-ind2@example.com", digital_id="10000000000000030"
    )
    patient = minor(digital_id="30000000000000026")
    relationship(father, patient, kind="FATHER")
    relationship(mother, patient, kind="MOTHER")
    verified_document(patient, father, date(2026, 1, 1))

    api_client.force_authenticate(user=father)
    father_view = api_client.get(guardian_archive(patient))
    api_client.force_authenticate(user=mother)
    mother_view = api_client.get(guardian_archive(patient))

    assert father_view.status_code == 200
    assert mother_view.status_code == 200
    for response in (father_view, mother_view):
        encoded = str(response.data)
        assert "m12-ind1@example.com" not in encoded
        assert "m12-ind2@example.com" not in encoded


def test_guardian_unconfirmed_bucket_and_filters(api_client):
    father = verified_guardian(
        email="m12-filter@example.com", digital_id="10000000000000031"
    )
    patient = minor(digital_id="30000000000000027")
    relationship(father, patient, kind="FATHER")
    verified_document(patient, father, date(2026, 3, 14))
    make_document(patient, father)
    api_client.force_authenticate(user=father)

    default = api_client.get(guardian_archive(patient))
    unconfirmed = api_client.get(f"{guardian_archive(patient)}?date_status=UNCONFIRMED")
    year = api_client.get(f"{guardian_archive(patient)}?year=2026")
    assert default.data["data"]["count"] == 1
    assert default.data["data"]["unconfirmed_date_count"] == 1
    assert unconfirmed.data["data"]["count"] == 1
    assert year.data["data"]["count"] == 1
