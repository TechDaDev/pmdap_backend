import io

import pytest
from django.core.files.base import ContentFile
from PIL import Image

from identities.extraction_store import store_extraction_result
from identities.models import IdentityExtractionJob
from identities.storage import private_identity_storage
from tests.test_minors_guardians import (
    MINORS,
    auth,
    create_verified_guardian,
    document_model,
    patient_model,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def identity_storage_dir(settings, tmp_path):
    settings.IDENTITY_FILE_ROOT = tmp_path / "private-identity"
    settings.IDENTITY_FILE_MAX_BYTES = 1024 * 1024


def _png_bytes():
    output = io.BytesIO()
    Image.new("RGB", (8, 8), color=(35, 80, 120)).save(output, format="PNG")
    return output.getvalue()


def _successful_card_job(user):
    job = IdentityExtractionJob.objects.create(
        user=user,
        document_type="UNIFIED_NATIONAL_CARD",
        status=IdentityExtractionJob.Status.SUCCESS,
    )
    job.front_key = f"extract_staging/{job.uuid}/front.png"
    job.back_key = f"extract_staging/{job.uuid}/back.png"
    private_identity_storage.save(job.front_key, ContentFile(_png_bytes()))
    private_identity_storage.save(job.back_key, ContentFile(_png_bytes()))
    job.save(update_fields=("front_key", "back_key", "updated_at"))
    store_extraction_result(
        job.uuid,
        {
            "document_type": "UNIFIED_NATIONAL_CARD",
            "extractor_version": "synthetic-m28.1",
            "fields": {
                "name": {"value": "SyntheticGiven"},
                "father_name": {"value": "SyntheticFather"},
                "grandfather_name": {"value": "SyntheticGrandfather"},
                "national_card_number": {"value": "100000000001"},
                "unique_card_body_number": {"value": "A10000001"},
                "family_number": {"value": "SYNTH-FAMILY-100"},
            },
            "warnings": [],
            "mrz": {"detected": False, "valid": False, "checks_passed": False},
        },
    )
    return job


def _payload(job):
    return {
        "name": "SyntheticGiven",
        "father_name": "SyntheticFather",
        "grandfather_name": "SyntheticGrandfather",
        "date_of_birth": "2015-05-10",
        "sex": "MALE",
        "nationality": "IQ",
        "blood_group": "O+",
        "relationship": "FATHER",
        "document_type": "UNIFIED_NATIONAL_CARD",
        "extraction_job_id": str(job.uuid),
    }


def test_minor_card_uses_structured_names_and_server_extraction(api_client):
    guardian, guardian_profile, _ = create_verified_guardian()
    job = _successful_card_job(guardian)
    auth(api_client, guardian)

    response = api_client.post(
        MINORS,
        _payload(job),
        format="multipart",
        HTTP_IDEMPOTENCY_KEY="m28-1-structured-extraction",
    )

    assert response.status_code == 201, response.data
    minor = patient_model().objects.exclude(pk=guardian_profile.pk).get()
    assert minor.full_name == "SyntheticGiven SyntheticFather SyntheticGrandfather"
    assert minor.given_name == "SyntheticGiven"
    assert minor.father_name == "SyntheticFather"
    assert minor.grandfather_name == "SyntheticGrandfather"
    card = document_model().objects.get(patient=minor)
    assert card.document_number == "100000000001"
    assert card.national_number == "100000000001"
    assert card.unique_card_body_number == "A10000001"
    assert card.family_number == "SYNTH-FAMILY-100"


def test_minor_card_rejects_client_forged_family_number(api_client):
    guardian, _, _ = create_verified_guardian(email="m28-1-forge@example.com")
    job = _successful_card_job(guardian)
    payload = _payload(job)
    payload["family_number"] = "FORGED-MATCH"
    auth(api_client, guardian)

    response = api_client.post(
        MINORS,
        payload,
        format="multipart",
        HTTP_IDEMPOTENCY_KEY="m28-1-family-forge",
    )

    assert response.status_code == 400
    assert "family_number" in response.data["error"]["details"]
    assert (
        not patient_model()
        .objects.filter(full_name__startswith="SyntheticGiven")
        .exists()
    )


def test_relationship_summaries_still_hide_identity_numbers(api_client):
    guardian, _, _ = create_verified_guardian(email="m28-1-list@example.com")
    job = _successful_card_job(guardian)
    auth(api_client, guardian)
    created = api_client.post(
        MINORS,
        _payload(job),
        format="multipart",
        HTTP_IDEMPOTENCY_KEY="m28-1-list-safe",
    )
    assert created.status_code == 201, created.data

    response = api_client.get("/api/v1/guardian-relationships/")
    rendered = str(response.data).lower()
    assert response.status_code == 200
    for forbidden in ("family_number", "national_number", "card_body"):
        assert forbidden not in rendered
