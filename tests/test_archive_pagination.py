from datetime import date, timedelta

import pytest

from tests.archive_helpers import verified_document
from tests.test_medical_documents_api import patient_user

pytestmark = pytest.mark.django_db

ARCHIVE = "/api/v1/archive/"


def authenticate(client, user):
    client.force_authenticate(user=user)


def collect_all(client, url):
    results = []
    next_url = url
    while next_url:
        response = client.get(next_url)
        assert response.status_code == 200
        body = response.data["data"]
        results.extend(body["results"])
        next_url = body["next"]
    return results


def test_pagination_has_no_duplicates_or_missing_and_stable_order(api_client):
    user, patient = patient_user()
    fixed_created = date(2026, 1, 1)
    documents = [
        verified_document(
            patient,
            user,
            date(2026, 1, 1) + timedelta(days=index),
            title=f"doc-{index}",
            created_at=fixed_created,
        )
        for index in range(45)
    ]
    expected = sorted(
        documents,
        key=lambda d: (d.document_date, d.created_at, d.uuid),
        reverse=True,
    )
    authenticate(api_client, user)

    first_pass = collect_all(api_client, ARCHIVE)
    assert len(first_pass) == 45
    assert len({r["uuid"] for r in first_pass}) == 45
    assert [r["uuid"] for r in first_pass] == [str(d.uuid) for d in expected]

    second_pass = collect_all(api_client, ARCHIVE)
    assert [r["uuid"] for r in second_pass] == [r["uuid"] for r in first_pass]


def test_identical_date_and_created_at_tie_break_is_stable(api_client):
    user, patient = patient_user()
    fixed_created = date(2026, 1, 1)
    shared_date = date(2026, 3, 14)
    documents = [
        verified_document(
            patient,
            user,
            shared_date,
            title=f"tie-{index}",
            created_at=fixed_created,
        )
        for index in range(45)
    ]
    expected = [
        str(d.uuid) for d in sorted(documents, key=lambda d: d.uuid, reverse=True)
    ]
    authenticate(api_client, user)

    collected = collect_all(api_client, ARCHIVE)
    assert len(collected) == 45
    assert [r["uuid"] for r in collected] == expected

    # Boundary check: page 2 starts exactly where page 1 ended.
    page_one = api_client.get(ARCHIVE).data["data"]
    page_two = api_client.get(page_one["next"]).data["data"]
    assert page_one["results"][-1]["uuid"] != page_two["results"][0]["uuid"]


def test_filter_and_pagination_combine(api_client):
    user, patient = patient_user()
    fixed_created = date(2026, 1, 1)
    for index in range(45):
        year = 2026 if index % 2 == 0 else 2025
        verified_document(
            patient,
            user,
            date(year, 6, 1) + timedelta(days=index % 28),
            title=f"doc-{index}",
            created_at=fixed_created,
        )
    authenticate(api_client, user)
    collected = collect_all(api_client, f"{ARCHIVE}?year=2026")
    assert len(collected) == 23
    assert {r["document_date"][:4] for r in collected} == {"2026"}
