"""Advisory identity extraction endpoint tests — synthetic images only.

Real identity images/values are never used; every fixture is invented and
labelled SYNTHETIC TEST DOCUMENT.
"""
import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image, ImageDraw
from rest_framework_simplejwt.tokens import RefreshToken

from identities import extraction
from identities.models import IdentityExtractionJob
from processing.ocr import OCRLine, OCREngineUnavailableError
from tests.factories import UserFactory

EXTRACT = "/api/v1/identity-documents/extract/"


def synthetic_png(text="SYNTHETIC TEST DOCUMENT 123456789012345"):
    img = Image.new("RGB", (420, 140), "white")
    draw = ImageDraw.Draw(img)
    draw.text((10, 10), text, fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return SimpleUploadedFile("synthetic.png", buf.getvalue(), content_type="image/png")


def _auth(api_client, user):
    api_client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}"
    )


@pytest.mark.django_db
def test_extract_requires_authentication(api_client):
    resp = api_client.post(EXTRACT, {})
    assert resp.status_code == 401


@pytest.mark.django_db
def test_extract_requires_patient(api_client):
    agent = UserFactory(role="IDENTITY_VERIFICATION_AGENT", status="ACTIVE")
    _auth(api_client, agent)
    resp = api_client.post(
        EXTRACT,
        {"document_type": "UNIFIED_NATIONAL_CARD", "front_image": synthetic_png()},
    )
    assert resp.status_code == 403


@pytest.mark.django_db
def test_extract_unsupported_type_rejected(api_client):
    user = UserFactory(status="ACTIVE")
    _auth(api_client, user)
    resp = api_client.post(
        EXTRACT,
        {"document_type": "BIRTH_DOCUMENT", "front_image": synthetic_png()},
    )
    assert resp.status_code == 400


@pytest.mark.django_db
def test_national_card_requires_back_image(api_client):
    user = UserFactory(status="ACTIVE")
    _auth(api_client, user)
    resp = api_client.post(
        EXTRACT,
        {"document_type": "UNIFIED_NATIONAL_CARD", "front_image": synthetic_png()},
    )
    assert resp.status_code == 400


@pytest.mark.django_db
@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
def test_extract_async_flow_returns_structured_result_without_db_record(
    api_client, monkeypatch
):
    from identities.models import IdentityDocument

    class FakeEngine:
        def extract_image(self, image):
            return FakeResult()

    class FakeResult:
        lines = (
            OCRLine("SYNTHETIC TEST DOCUMENT", 0.99),
            OCRLine("NATIONAL NO: 012345678901234", 0.98),
            OCRLine("FAMILY NO: 1234", 0.9),
        )

    monkeypatch.setattr("identities.tasks.PaddleOCREngine", FakeEngine)
    user = UserFactory(status="ACTIVE")
    before = IdentityDocument.objects.count()
    _auth(api_client, user)

    resp = api_client.post(
        EXTRACT,
        {
            "document_type": "UNIFIED_NATIONAL_CARD",
            "front_image": synthetic_png(),
            "back_image": synthetic_png(),
        },
    )
    assert resp.status_code == 202
    job_id = resp.json()["data"]["job_id"]
    assert resp.json()["data"]["status"] == "PENDING"

    job = IdentityExtractionJob.objects.get(uuid=job_id)
    assert job.status == IdentityExtractionJob.Status.SUCCESS  # eager worker ran

    resp = api_client.get(f"{EXTRACT}{job_id}/")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "SUCCESS"
    assert data["document_type"] == "UNIFIED_NATIONAL_CARD"
    assert data["extractor_version"] == "identity-v1"
    assert data["fields"]["national_number"]["value"] == "012345678901234"
    assert data["mrz"]["detected"] is False
    # No raw OCR text / image bytes anywhere in the response.
    raw = resp.content.decode()
    assert "SYNTHETIC TEST DOCUMENT" not in raw
    assert IdentityDocument.objects.count() == before
    # Job row consumed (nothing persisted).
    assert not IdentityExtractionJob.objects.filter(uuid=job_id).exists()
    # Staging keys cleared from the job row.
    assert job.front_key == ""
    assert job.back_key == ""


@pytest.mark.django_db
@override_settings(CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
def test_extract_ocr_unavailable_degrades_to_failed(api_client, monkeypatch):
    def raise_unavailable(self, *, pipeline=None):
        raise OCREngineUnavailableError("no paddle")

    monkeypatch.setattr("identities.tasks.PaddleOCREngine.__init__", raise_unavailable)
    user = UserFactory(status="ACTIVE")
    _auth(api_client, user)

    resp = api_client.post(
        EXTRACT,
        {
            "document_type": "UNIFIED_NATIONAL_CARD",
            "front_image": synthetic_png(),
            "back_image": synthetic_png(),
        },
    )
    assert resp.status_code == 202
    job_id = resp.json()["data"]["job_id"]

    resp = api_client.get(f"{EXTRACT}{job_id}/")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "FAILED"
    assert data["error_code"] == "OCR_UNAVAILABLE"
    assert not IdentityExtractionJob.objects.filter(uuid=job_id).exists()


@pytest.mark.django_db
def test_extract_job_ownership_enforced(api_client):
    owner = UserFactory(status="ACTIVE")
    other = UserFactory(status="ACTIVE")
    job = IdentityExtractionJob.objects.create(
        user=owner, document_type="UNIFIED_NATIONAL_CARD"
    )
    _auth(api_client, other)
    resp = api_client.get(f"{EXTRACT}{job.uuid}/")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_extract_status_unknown_job_404(api_client):
    import uuid as uuid_mod

    user = UserFactory(status="ACTIVE")
    _auth(api_client, user)
    resp = api_client.get(f"{EXTRACT}{uuid_mod.uuid4()}/")
    assert resp.status_code == 404


def test_extract_national_card_fields_deterministic():
    lines = [
        "العراق",
        "123456789012345",
        "1234",
        "DOC98765",
    ]
    fields, warnings, mrz_summary = extraction.extract_identity(
        "UNIFIED_NATIONAL_CARD", lines
    )
    assert fields["issuing_country"] == {"value": "IQ", "confidence": 1.0, "source": "DOCUMENT_TYPE"}
    assert fields["national_number"]["value"] == "123456789012345"
    assert fields["family_number"]["value"] == "1234"
    assert "document_number" in fields
    assert mrz_summary["detected"] is False


def test_extract_passport_from_synthetic_mrz_lines():
    from identities import mrz as mrz_mod

    def cd(s):
        return str(mrz_mod.check_digit(s))

    doc = "AB123456".ljust(9, "<")
    line1 = (
        "P<UTO"
        + doc
        + cd(doc)
        + "UTO"
        + "<"
        + "900101"
        + cd("900101")
        + "M"
        + "<<"
        + "301231"
        + cd("301231")
        + "00000000"
    )
    line2 = ("DOE<<JOHN").ljust(44, "<")
    lines = [
        "Date of Issue: 2020-05-05",
        line1,
        line2,
    ]
    fields, warnings, mrz_summary = extraction.extract_identity("PASSPORT", lines)
    assert fields["document_number"]["value"] == "AB123456"
    assert fields["issuing_country"]["value"] == "UTO"
    assert fields["expiry_date"]["value"].startswith("2030")
    assert fields["issue_date"]["value"] == "2020-05-05"
    assert fields["issue_date"]["source"] == "OCR"
    assert mrz_summary["detected"] is True


def test_extract_no_text_returns_structured():
    fields, warnings, mrz_summary = extraction.extract_identity("PASSPORT", [])
    assert fields == {}
    assert warnings  # NO_TEXT_DETECTED / OCR_UNAVAILABLE
    assert mrz_summary["detected"] is False
