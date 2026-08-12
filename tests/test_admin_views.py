"""Admin HTML privacy checks.

Renders admin changelist pages as a superuser and asserts that sensitive
identity / medical values never appear in the HTML, even though the underlying
records exist and are searchable.

This is the runtime complement to test_admin_registration.py (which asserts
list_display/fields configuration).
"""
import io
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from tests.factories import UserFactory

# Deliberately distinctive synthetic values that MUST never render in admin.
SECRET_DOCUMENT_NUMBER = "SECRET-DOCNUM-9911-XX"
SECRET_NATIONAL_NUMBER = "SECRET-NATNUM-7711-XX"
SECRET_OCR_TEXT = "SECRET-OCR-BODY-ABC123"
SECRET_DATE_CONTEXT = "SECRET-DATE-CTX-456"


@pytest.fixture(autouse=True)
def private_identity_storage(settings, tmp_path):
    settings.IDENTITY_FILE_ROOT = tmp_path / "private-identity"


@pytest.fixture
def superuser():
    user = UserFactory(email="admin@example.com", status="ACTIVE")
    user.is_staff = True
    user.is_superuser = True
    user.save(update_fields=("is_staff", "is_superuser"))
    return user


def make_identity_document():
    """Create a patient + pending identity document with secret-looking values."""
    from identities.models import IdentityDocument, IdentityDocumentEvent, IdentityFile
    from patients.services import create_patient_profile

    user = UserFactory(email="patient-secret@example.com", status="ACTIVE")
    profile = create_patient_profile(
        user=user,
        full_name="Secret Test Patient",
        date_of_birth="1990-01-01",
        sex="MALE",
        nationality="IQ",
        blood_group="O+",
    )

    raw = io.BytesIO()
    Image.new("RGB", (8, 8)).save(raw, format="JPEG")
    upload = SimpleUploadedFile("front.jpg", raw.getvalue(), content_type="image/jpeg")
    from identities.services import persist_identity_upload

    front = persist_identity_upload(upload)

    doc = IdentityDocument.objects.create(
        patient=profile,
        document_type=IdentityDocument.DocumentType.UNIFIED_NATIONAL_CARD,
        document_number=SECRET_DOCUMENT_NUMBER,
        national_number=SECRET_NATIONAL_NUMBER,
        family_number="FAM-SECRET",
        issuing_country="IQ",
        front_image=front,
    )
    IdentityDocumentEvent.objects.create(
        document=doc,
        event_type=IdentityDocumentEvent.EventType.UPLOADED,
        actor=user,
    )
    return doc, user


def make_processing_rows():
    """Create a medical document + DocumentText with secret OCR body."""
    import uuid as _uuid

    from documents.models import MedicalDocument, StoredFile
    from patients.services import create_patient_profile
    from processing.models import DateCandidate, DocumentText, DocumentTextPage

    user = UserFactory(email="doc-secret@example.com", status="ACTIVE")
    profile = create_patient_profile(
        user=user,
        full_name="Doc Secret Patient",
        date_of_birth="1988-03-03",
        sex="FEMALE",
        nationality="IQ",
        blood_group="A+",
    )
    stored = StoredFile.objects.create(
        file=SimpleUploadedFile("scan.pdf", b"%PDF-1.4 secret", content_type="application/pdf"),
        original_filename="scan.pdf",
        mime_type="application/pdf",
        size_bytes=12,
        sha256="a" * 64,
        page_count=1,
    )
    doc = MedicalDocument.objects.create(
        patient=profile,
        uploaded_by=user,
        stored_file=stored,
        content_sha256="b" * 64,
        document_type=MedicalDocument.DocumentType.MEDICAL_REPORT,
        title="Secret Report",
    )
    text = DocumentText.objects.create(
        document=doc,
        text=SECRET_OCR_TEXT,
        page_count=1,
        character_count=len(SECRET_OCR_TEXT),
        meaningful_character_count=len(SECRET_OCR_TEXT),
        usable=True,
        usability_reason="ok",
        extraction_method=DocumentText.ExtractionMethod.OCR,
        extractor_name="paddle",
        extractor_version="1",
        pipeline_version="p1",
    )
    DocumentTextPage.objects.create(
        document_text=text,
        page_number=1,
        text=SECRET_OCR_TEXT,
        native_text=SECRET_OCR_TEXT,
        ocr_text=SECRET_OCR_TEXT,
        meaningful_character_count=len(SECRET_OCR_TEXT),
        effective_source=DocumentTextPage.EffectiveSource.OCR,
        ocr_mean_confidence=0.9,
    )
    DateCandidate.objects.create(
        document=doc,
        detected_date="2024-05-06",
        raw_value=SECRET_DATE_CONTEXT,
        normalized_value="2024-05-06",
        candidate_type=DateCandidate.CandidateType.REPORT_DATE,
        score=0.99,
        page_number=1,
        context=SECRET_DATE_CONTEXT,
        source=DateCandidate.Source.OCR,
        occurrence_index=0,
        parsing_rule="iso",
        pipeline_version="p1",
        is_suggested=True,
    )
    return doc


@pytest.mark.django_db
def test_identity_admin_list_does_not_render_sensitive_numbers(client, superuser):
    make_identity_document()
    client.force_login(superuser)

    response = client.get("/admin/identities/identitydocument/")

    assert response.status_code == 200
    html = response.content.decode()
    assert SECRET_DOCUMENT_NUMBER not in html
    assert SECRET_NATIONAL_NUMBER not in html
    assert "FAM-SECRET" not in html


@pytest.mark.django_db
def test_processing_admin_pages_do_not_render_ocr_text(client, superuser):
    make_processing_rows()
    client.force_login(superuser)

    for path in (
        "/admin/processing/documenttext/",
        "/admin/processing/documenttextpage/",
        "/admin/processing/datecandidate/",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        html = response.content.decode()
        assert SECRET_OCR_TEXT not in html, path
        assert SECRET_DATE_CONTEXT not in html, path


@pytest.mark.django_db
def test_identity_document_admin_detail_is_read_only(client, superuser):
    """Detail page is effectively read-only: no save/submit controls and no
    verification transition widgets, even for a superuser."""
    doc, _ = make_identity_document()
    client.force_login(superuser)

    response = client.get(f"/admin/identities/identitydocument/{doc.uuid}/change/")

    assert response.status_code == 200
    html = response.content.decode()
    assert 'name="_save"' not in html
    assert 'name="verification_status"' not in html
    assert 'name="status"' not in html
