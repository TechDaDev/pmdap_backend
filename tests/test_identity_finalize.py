"""Finalize-via-extraction-job tests (single-upload identity submission).

The client uploads images once at extract time; the final submit carries only
corrected strings + extraction_job_id. These tests cover ownership, single-use,
expiry, storage promotion, rollback and the legacy multipart path.
"""
import io

import pytest
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image
from rest_framework_simplejwt.tokens import RefreshToken

from identities.exceptions import IdentityFileStorageFailed
from identities.models import IdentityExtractionJob
from identities.storage import private_identity_storage
from tests.factories import UserFactory

COLLECTION = "/api/v1/identity-documents/"


@pytest.fixture(autouse=True)
def identity_storage_dir(settings, tmp_path):
    settings.IDENTITY_FILE_ROOT = tmp_path / "private-identity"
    settings.IDENTITY_FILE_MAX_BYTES = 1024 * 1024


def synthetic_png():
    out = io.BytesIO()
    Image.new("RGB", (8, 8), color=(35, 80, 120)).save(out, format="PNG")
    out.seek(0)
    return SimpleUploadedFile("syn.png", out.getvalue(), content_type="image/png")


def _auth(api_client, user):
    api_client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}"
    )


def create_patient(*, email="patient@example.com"):
    from patients.services import create_patient_profile

    user = UserFactory(email=email, status="ACTIVE")
    profile = create_patient_profile(
        user=user,
        full_name="Layla Hassan",
        date_of_birth="1992-02-29",
        sex="FEMALE",
        nationality="IQ",
        blood_group="A+",
    )
    return user, profile


def make_success_job(user, document_type="UNIFIED_NATIONAL_CARD", with_back=True):
    job = IdentityExtractionJob.objects.create(
        user=user, document_type=document_type, status=IdentityExtractionJob.Status.SUCCESS
    )
    front_key = f"extract_staging/{job.uuid}/front.png"
    private_identity_storage.save(front_key, ContentFile(synthetic_png().read()))
    back_key = ""
    if with_back:
        back_key = f"extract_staging/{job.uuid}/back.png"
        private_identity_storage.save(back_key, ContentFile(synthetic_png().read()))
    job.front_key = front_key
    job.back_key = back_key
    job.save(update_fields=["front_key", "back_key"])
    return job


def nc_payload(job, **overrides):
    payload = {
        "document_type": "UNIFIED_NATIONAL_CARD",
        "document_number": "CARD-001",
        "national_number": "NAT-001",
        "family_number": "FAM-001",
        "issuing_country": "IQ",
        "extraction_job_id": str(job.uuid),
    }
    payload.update(overrides)
    return payload


def finalize(api_client, user, payload):
    _auth(api_client, user)
    return api_client.post(COLLECTION, payload, format="json")


def identity_document_model():
    from django.apps import apps

    return apps.get_model("identities", "IdentityDocument")


@pytest.mark.django_db
def test_finalize_national_card_via_job(api_client):
    user, profile = create_patient()
    job = make_success_job(user)

    response = finalize(api_client, user, nc_payload(job))

    assert response.status_code == 201
    doc = identity_document_model().objects.get()
    assert doc.patient == profile
    assert doc.verification_status == "PENDING"
    assert doc.status == "CURRENT"
    assert doc.front_image_id is not None
    assert doc.back_image_id is not None
    profile.refresh_from_db()
    assert profile.identity_status == "PENDING_VERIFICATION"
    # Event + audit created.
    assert doc.events.filter(event_type="IDENTITY_DOCUMENT_UPLOADED").exists()
    from audit.models import AuditLog

    assert AuditLog.objects.filter(
        resource_uuid=doc.uuid, action="IDENTITY_DOCUMENT_UPLOADED"
    ).exists()
    # Staging + cache + job consumed.
    assert not private_identity_storage.exists(job.front_key)
    assert not private_identity_storage.exists(job.back_key)
    assert not IdentityExtractionJob.objects.filter(uuid=job.uuid).exists()
    from identities.extraction_store import read_extraction_result

    assert read_extraction_result(job.uuid) is None


@pytest.mark.django_db
def test_finalize_passport_via_job_promotes_front_only(api_client):
    user, _ = create_patient()
    job = make_success_job(user, document_type="PASSPORT", with_back=False)

    payload = {
        "document_type": "PASSPORT",
        "document_number": "P1234567",
        "issuing_country": "IQ",
        "issue_date": "2024-01-01",
        "expiry_date": "2034-01-01",
        "extraction_job_id": str(job.uuid),
    }
    response = finalize(api_client, user, payload)

    assert response.status_code == 201
    doc = identity_document_model().objects.get()
    assert doc.document_type == "PASSPORT"
    assert doc.front_image_id is not None
    assert doc.back_image_id is None


@pytest.mark.django_db
def test_finalize_job_owned_by_another_user_404(api_client):
    user, _ = create_patient()
    other, _ = create_patient(email="other@example.com")
    job = make_success_job(other)

    response = finalize(api_client, user, nc_payload(job))

    assert response.status_code == 404
    assert not identity_document_model().objects.exists()
    # Other user's staging untouched; job still finalizable by owner.
    assert private_identity_storage.exists(job.front_key)


@pytest.mark.django_db
def test_finalize_wrong_document_type_400(api_client):
    user, _ = create_patient()
    job = make_success_job(user)  # UNIFIED_NATIONAL_CARD job
    response = finalize(
        api_client,
        user,
        {
            "document_type": "PASSPORT",
            "document_number": "P1",
            "issuing_country": "IQ",
            "issue_date": "2024-01-01",
            "expiry_date": "2034-01-01",
            "extraction_job_id": str(job.uuid),
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "extraction_job_mismatch"
    assert not identity_document_model().objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status", [IdentityExtractionJob.Status.PENDING, IdentityExtractionJob.Status.FAILED]
)
def test_finalize_non_success_job_409(api_client, status):
    user, _ = create_patient()
    job = IdentityExtractionJob.objects.create(
        user=user, document_type="UNIFIED_NATIONAL_CARD", status=status
    )
    response = finalize(api_client, user, nc_payload(job))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "extraction_job_conflict"
    assert not identity_document_model().objects.exists()


@pytest.mark.django_db
def test_finalize_expired_job_409(api_client):
    user, _ = create_patient()
    job = IdentityExtractionJob.objects.create(
        user=user, document_type="UNIFIED_NATIONAL_CARD", status=IdentityExtractionJob.Status.EXPIRED
    )
    response = finalize(api_client, user, nc_payload(job))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "extraction_job_expired"
    assert not identity_document_model().objects.exists()


@pytest.mark.django_db
def test_job_reused_after_finalize_rejected(api_client):
    user, _ = create_patient()
    job = make_success_job(user)
    assert finalize(api_client, user, nc_payload(job)).status_code == 201
    assert identity_document_model().objects.count() == 1

    # Job was consumed (deleted) → second attempt is a safe 404, no duplicate.
    response = finalize(api_client, user, nc_payload(job))
    assert response.status_code == 404
    assert identity_document_model().objects.count() == 1


@pytest.mark.django_db
def test_both_extraction_job_and_images_400(api_client):
    user, _ = create_patient()
    job = make_success_job(user)
    _auth(api_client, user)
    # Multipart with BOTH extraction_job_id and an image file → ambiguous.
    response = api_client.post(
        COLLECTION,
        {
            "document_type": "UNIFIED_NATIONAL_CARD",
            "document_number": "CARD-001",
            "national_number": "NAT-001",
            "family_number": "FAM-001",
            "issuing_country": "IQ",
            "extraction_job_id": str(job.uuid),
            "front_image": synthetic_png(),
        },
        format="multipart",
    )
    assert response.status_code == 400
    assert "extraction_job_id" in response.json()["error"]["details"]
    assert not identity_document_model().objects.exists()


@pytest.mark.django_db
def test_neither_job_nor_images_400(api_client):
    user, _ = create_patient()
    response = finalize(
        api_client,
        user,
        {
            "document_type": "UNIFIED_NATIONAL_CARD",
            "document_number": "CARD-001",
            "national_number": "NAT-001",
            "family_number": "FAM-001",
            "issuing_country": "IQ",
        },
    )
    assert response.status_code == 400
    assert "front_image" in response.json()["error"]["details"]
    assert not identity_document_model().objects.exists()


@pytest.mark.django_db
def test_storage_failure_503_rollback_no_orphan(api_client, monkeypatch):
    user, _ = create_patient()
    job = make_success_job(user)

    def boom(job_, key, name):
        raise IdentityFileStorageFailed()

    monkeypatch.setattr("identities.services._read_staged_validated", boom)
    response = finalize(api_client, user, nc_payload(job))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "identity_file_storage_failed"
    assert not identity_document_model().objects.exists()
    # Job intact (staging kept) — client can retry.
    assert IdentityExtractionJob.objects.filter(uuid=job.uuid).exists()
    job.refresh_from_db()
    assert job.status == IdentityExtractionJob.Status.SUCCESS
    assert private_identity_storage.exists(job.front_key)


@pytest.mark.django_db
def test_partial_promotion_failure_removes_orphans(api_client, monkeypatch):
    user, _ = create_patient()
    job = make_success_job(user)
    from identities.models import IdentityFile

    real_persist = __import__(
        "identities.services", fromlist=["_persist_file"]
    )._persist_file
    calls = {"n": 0}

    def flaky_persist(validated):
        calls["n"] += 1
        if calls["n"] == 2:  # back promotion fails after front succeeded
            raise IdentityFileStorageFailed()
        return real_persist(validated)

    monkeypatch.setattr("identities.services._persist_file", flaky_persist)
    response = finalize(api_client, user, nc_payload(job))

    assert response.status_code == 503
    assert not identity_document_model().objects.exists()
    # No orphan IdentityFile rows (front was promoted then rolled back).
    assert IdentityFile.objects.count() == 0
    job.refresh_from_db()
    assert job.status == IdentityExtractionJob.Status.SUCCESS
    assert private_identity_storage.exists(job.front_key)
    assert private_identity_storage.exists(job.back_key)


@pytest.mark.django_db
def test_expiry_cleanup_removes_abandoned_success_job(settings):
    from datetime import timedelta

    from django.utils import timezone

    from identities.extraction_store import store_extraction_result
    from identities.tasks import cleanup_identity_extraction_jobs

    user, _ = create_patient()
    job = make_success_job(user)
    store_extraction_result(job.uuid, {"fields": {}})
    # Age the job beyond the staging TTL.
    IdentityExtractionJob.objects.filter(uuid=job.uuid).update(
        updated_at=timezone.now() - timedelta(seconds=settings.IDENTITY_STAGING_TTL_SECONDS + 10)
    )

    cleanup_identity_extraction_jobs(str(job.uuid))

    assert not IdentityExtractionJob.objects.filter(uuid=job.uuid).exists()
    assert not private_identity_storage.exists(job.front_key)
    assert not private_identity_storage.exists(job.back_key)
    from identities.extraction_store import read_extraction_result

    assert read_extraction_result(job.uuid) is None


@pytest.mark.django_db
def test_expiry_cleanup_sweep_removes_abandoned_jobs(settings):
    from datetime import timedelta

    from django.utils import timezone

    from identities.tasks import cleanup_identity_extraction_jobs

    user, _ = create_patient()
    job = make_success_job(user)
    IdentityExtractionJob.objects.filter(uuid=job.uuid).update(
        updated_at=timezone.now() - timedelta(seconds=settings.IDENTITY_STAGING_TTL_SECONDS + 10)
    )

    cleanup_identity_extraction_jobs()  # sweep (no uuid)

    assert not IdentityExtractionJob.objects.filter(uuid=job.uuid).exists()
    assert not private_identity_storage.exists(job.front_key)


@pytest.mark.django_db
def test_expiry_cleanup_skips_finalized_and_fresh_jobs(settings):
    from identities.tasks import cleanup_identity_extraction_jobs

    user, _ = create_patient()
    fresh = make_success_job(user)  # updated_at = now → not expired
    finalized = make_success_job(user)
    IdentityExtractionJob.objects.filter(uuid=finalized.uuid).update(
        status=IdentityExtractionJob.Status.FINALIZED
    )

    cleanup_identity_extraction_jobs(str(fresh.uuid))
    cleanup_identity_extraction_jobs(str(finalized.uuid))

    assert IdentityExtractionJob.objects.filter(uuid=fresh.uuid).exists()
    assert IdentityExtractionJob.objects.filter(uuid=finalized.uuid).exists()
    assert private_identity_storage.exists(fresh.front_key)


@pytest.mark.django_db
def test_legacy_multipart_submit_still_passes(api_client):
    user, _ = create_patient()
    _auth(api_client, user)
    response = api_client.post(
        COLLECTION,
        {
            "document_type": "UNIFIED_NATIONAL_CARD",
            "document_number": "CARD-001",
            "national_number": "NAT-001",
            "family_number": "FAM-001",
            "issuing_country": "IQ",
            "front_image": synthetic_png(),
            "back_image": synthetic_png(),
        },
        format="multipart",
    )
    assert response.status_code == 201
    assert identity_document_model().objects.count() == 1


@pytest.mark.django_db
def test_replacement_via_extraction_job(api_client):
    from identities.models import IdentityDocument, IdentityFile

    user, _ = create_patient()
    _auth(api_client, user)
    # Existing VERIFIED current National Card (the replace source).
    front = IdentityFile.objects.create(
        original_name="old.jpg", media_type="image/jpeg", size=1, sha256="0" * 64
    )
    src = IdentityDocument.objects.create(
        patient=user.patient_profile,
        document_type="UNIFIED_NATIONAL_CARD",
        document_number="OLD-1",
        national_number="OLD-N",
        family_number="OLD-F",
        issuing_country="IQ",
        front_image=front,
        verification_status="VERIFIED",
        status="CURRENT",
    )
    job = make_success_job(user)

    payload = nc_payload(job, document_number="NEW-1")
    _auth(api_client, user)
    response = api_client.post(
        f"{COLLECTION}{src.uuid}/replace/", payload, format="json"
    )

    assert response.status_code == 201
    doc = identity_document_model().objects.exclude(pk=src.pk).get()
    assert doc.replaces_id == src.pk
    assert doc.document_number == "NEW-1"
    assert not IdentityExtractionJob.objects.filter(uuid=job.uuid).exists()
