"""Extracted-content (narrative) endpoint tests — extraction + security.

Synthetic values only. Verifies narrative sectioning on several report types,
conservative header/footer filtering, owner/IDOR/role isolation, response
safety (no geometry / no OCR confidence), LAB routing, and minor-guardian
scope. Canonical OCR is read-only; nothing is mutated.
"""
from datetime import date

import pytest

from accounts.models import User
from documents.models import MedicalDocument, StoredFile
from documents.narrative import extract_narrative
from patients.models import PatientProfile
from processing.models import DocumentText, DocumentTextPage, DocumentTextSpan

pytestmark = pytest.mark.django_db

CONTENT_PATH = "/api/v1/documents/{uuid}/extracted-content/"
MINOR_CONTENT_PATH = "/api/v1/minors/{minor}/documents/{doc}/extracted-content/"


def make_patient(*, email, digital_id):
    user = User.objects.create_user(
        email=email,
        password="A-complex-password-2026!",
        status=User.Status.ACTIVE,
    )
    profile = PatientProfile.objects.create(
        user=user,
        digital_id=digital_id,
        full_name="Synthetic Patient",
        date_of_birth=date(1990, 1, 2),
        sex=PatientProfile.Sex.UNSPECIFIED,
        nationality="IQ",
    )
    return user, profile


def make_document(patient, user, *, document_type="RADIOLOGY", status="TEXT_EXTRACTED"):
    stored = StoredFile.objects.create(
        file=f"medical/{patient.digital_id}.jpg",
        original_filename="report.jpg",
        mime_type="image/jpeg",
        size_bytes=123,
        sha256="c" * 64,
        page_count=1,
        integrity_status=StoredFile.IntegrityStatus.VALID,
        malware_scan_status=StoredFile.MalwareScanStatus.CLEAN,
    )
    document = MedicalDocument.objects.create(
        patient=patient,
        uploaded_by=user,
        stored_file=stored,
        content_sha256="d" * 64,
        document_type=document_type,
        processing_status=status,
    )
    DocumentText.objects.create(
        document=document,
        text="Synthetic body",
        page_count=1,
        character_count=13,
        meaningful_character_count=13,
        usable=True,
        usability_reason="ok",
        has_pages_requiring_ocr=False,
        extraction_method=DocumentText.ExtractionMethod.OCR,
        extractor_name="paddleocr",
        extractor_version="3.7.0",
        pipeline_version="m8-ocr-v1",
    )
    return document


# (y_min, y_max, x_min, x_max, confidence, text) — synthetic only.
def build_spans(document, lines):
    page = DocumentTextPage.objects.create(
        document_text=document.document_text,
        page_number=1,
        text="Synthetic",
        ocr_text="Synthetic",
        meaningful_character_count=9,
    )
    for seq, (y0, y1, x0, x1, conf, text) in enumerate(lines):
        DocumentTextSpan.objects.create(
            document_text_page=page,
            sequence=seq,
            text=text,
            confidence=conf,
            x_min=x0,
            y_min=y0,
            x_max=x1,
            y_max=y1,
            source="OCR",
            page_width=320,
            page_height=120,
        )
    return page


def radiology_lines(title="ABDOMINAL US"):
    return [
        (0.04, 0.07, 0.01, 0.50, 0.97, "ALWARKAA-RADIOLOGY CENTER"),
        (0.07, 0.10, 0.77, 0.85, 1.00, "الدكتور"),
        (0.09, 0.11, 0.13, 0.38, 0.99, "ALWARKAA RADIOLOGY CENTER"),
        (0.09, 0.12, 0.73, 0.90, 0.97, "بهجت عبد هانى"),
        (0.24, 0.26, 0.16, 0.38, 0.73, "التاريخ٢٠٢١٠١٠٢"),
        (0.24, 0.26, 0.43, 0.52, 0.86, "٤٠ سنة"),
        (0.23, 0.26, 0.67, 0.95, 0.94, "اسم المريض أسامة إسماعيل"),
        (0.31, 0.33, 0.11, 0.33, 0.99, title),
        (0.35, 0.38, 0.08, 0.91, 0.99, "Liver is of normal size showing normal texture."),
        (0.38, 0.41, 0.08, 0.94, 0.99, "GB is distended, no stone, no cholecystitis."),
        (0.41, 0.44, 0.11, 0.66, 0.98, "Pancreas is normal in size and texture."),
        (0.44, 0.47, 0.09, 0.88, 0.99, "Spleen is normal in size and texture."),
        (0.47, 0.50, 0.09, 0.94, 0.97, "Right kidney: normal PCS, no stone, no SOL."),
        (0.50, 0.53, 0.11, 0.92, 0.99, "Left kidney: normal PCS, no SOL."),
        (0.65, 0.68, 0.11, 0.88, 0.98, "UB is distended, normal wall thickness."),
        (0.69, 0.71, 0.08, 0.93, 0.97, "Prostate: 35 cc, homogeneous texture."),
        (0.72, 0.74, 0.08, 0.66, 0.97, "No free fluid in the abdominal cavities."),
        (0.75, 0.77, 0.09, 0.64, 0.99, "No Para aortic LAP."),
        (0.85, 0.88, 0.67, 0.84, 0.97, "Dr.Behjet Hani"),
        (0.87, 0.89, 0.65, 0.92, 0.99, "DMRD, FIMBS, RANZCR-Australia"),
        (0.90, 0.93, 0.58, 0.78, 0.99, "بهجت عبد هاني"),
        (0.92, 0.95, 0.60, 0.78, 0.93, "دكتوراه اشعة تشغيصي"),
        (0.98, 1.00, 0.27, 0.75, 0.92, "محمع الوركاء الطيي قرب مطعم ستي سنتر"),
    ]


def authenticate(client, user):
    client.force_authenticate(user=user)


# --------------------------------------------------------------------------- #
# Narrative extraction (pure)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "title,expected_body",
    [
        ("ABDOMINAL US", "Liver is of normal size showing normal texture."),
        ("CHEST X-RAY", "No focal lesion is seen in either lung field."),
        ("CT HEAD", "Midline structures are normal."),
        ("MRI BRAIN", "The ventricular system is normal."),
    ],
)
def test_narrative_extraction_single_section(title, expected_body):
    user, profile = make_patient(email="n1@example.com", digital_id="1" * 17)
    document = make_document(profile, user)
    lines = radiology_lines(title=title)
    # personalize one body line per report type (full sentence, ends with '.')
    body_line = (0.38, 0.41, 0.08, 0.94, 0.99, expected_body)
    lines[9] = body_line
    build_spans(document, lines)

    sections = extract_narrative(document)
    assert len(sections) == 1
    assert sections[0].heading == title
    assert expected_body in sections[0].body
    # header/footer noise filtered
    joined = sections[0].body + sections[0].heading
    for noise in ("CENTER", "البيلسان", "اسم المريض", "دكتوراه", "مطعم", "المرسل"):
        assert noise not in joined
    # page/sequence present, no geometry
    assert sections[0].page_number == 1
    assert sections[0].sequence >= 0


def test_narrative_multiple_sections_headings():
    user, profile = make_patient(email="n2@example.com", digital_id="2" * 17)
    document = make_document(profile, user)
    lines = radiology_lines(title="FINDINGS")
    # add a second heading + body below the first block
    lines.append((0.80, 0.83, 0.08, 0.40, 0.99, "IMPRESSION"))
    lines.append((0.84, 0.87, 0.08, 0.90, 0.98, "Normal study. No acute findings."))
    build_spans(document, lines)

    sections = extract_narrative(document)
    assert len(sections) == 2
    assert sections[0].heading == "FINDINGS"
    assert sections[1].heading == "IMPRESSION"
    assert "No acute findings" in sections[1].body


def test_narrative_no_text_returns_empty():
    user, profile = make_patient(email="n3@example.com", digital_id="3" * 17)
    document = make_document(profile, user)
    document.document_text.delete()
    document.refresh_from_db()
    assert extract_narrative(document) == []


def test_narrative_long_body_never_dropped_by_fringe_filter():
    user, profile = make_patient(email="n4@example.com", digital_id="4" * 17)
    document = make_document(profile, user)
    lines = radiology_lines(title="CT ABDOMEN")
    # insert many body lines so the block is far larger than any fringe
    body_block = []
    for i in range(12):
        body_block.append(
            (0.36 + i * 0.03, 0.38 + i * 0.03, 0.08, 0.9, 0.99, f"Finding line {i} with details.")
        )
    lines = lines[:8] + body_block + lines[18:]
    build_spans(document, lines)

    sections = extract_narrative(document)
    assert len(sections) == 1
    assert sections[0].heading == "CT ABDOMEN"
    assert "Finding line 11 with details." in sections[0].body


# --------------------------------------------------------------------------- #
# API: contract + security
# --------------------------------------------------------------------------- #


def test_owner_gets_narrative_sections_without_geometry(api_client):
    user, profile = make_patient(email="o1@example.com", digital_id="5" * 17)
    document = make_document(profile, user)
    build_spans(document, radiology_lines())

    authenticate(api_client, user)
    response = api_client.get(CONTENT_PATH.format(uuid=document.uuid))

    assert response.status_code == 200
    data = response.data["data"]
    assert str(data["document_uuid"]) == str(document.uuid)
    assert data["document_type"] == "RADIOLOGY"
    assert data["content_kind"] == "NARRATIVE"
    assert data["status"] == "COMPLETED"
    assert len(data["sections"]) == 1
    section = data["sections"][0]
    assert section["heading"] == "ABDOMINAL US"
    assert "Liver is of normal size" in section["body"]
    # no geometry / OCR internals leak
    encoded = str(data)
    for forbidden in ("x_min", "x_max", "y_min", "y_max", "confidence", "sha256"):
        assert forbidden not in encoded


def test_laboratory_document_routes_to_lab_kind(api_client):
    from labs.models import LabReportExtraction

    user, profile = make_patient(email="o2@example.com", digital_id="6" * 17)
    document = make_document(profile, user, document_type="LABORATORY")
    build_spans(document, radiology_lines(title="IGNORED"))
    LabReportExtraction.objects.create(
        document=document, pipeline_version="lab-v2", status="COMPLETED", result_count=1
    )

    authenticate(api_client, user)
    response = api_client.get(CONTENT_PATH.format(uuid=document.uuid))

    assert response.status_code == 200
    data = response.data["data"]
    assert data["content_kind"] == "LAB"
    assert data["status"] == "COMPLETED"
    assert data["sections"] == []


def test_other_patient_document_is_404(api_client):
    owner, owner_profile = make_patient(email="o3@example.com", digital_id="7" * 17)
    other, _ = make_patient(email="o4@example.com", digital_id="8" * 17)
    document = make_document(owner_profile, owner)
    build_spans(document, radiology_lines())

    authenticate(api_client, other)
    response = api_client.get(CONTENT_PATH.format(uuid=document.uuid))

    assert response.status_code == 404
    assert response.data["error"]["code"] == "medical_document_not_found"


def test_unauthenticated_is_401(api_client):
    response = api_client.get(
        CONTENT_PATH.format(uuid="00000000-0000-0000-0000-000000000000")
    )
    assert response.status_code in (401, 403)


def test_verification_agent_denied(api_client):
    agent = User.objects.create_user(
        email="verification-agent2@example.com",
        password="A-complex-password-2026!",
        status=User.Status.ACTIVE,
        role=User.Role.IDENTITY_VERIFICATION_AGENT,
    )
    owner, owner_profile = make_patient(email="o5@example.com", digital_id="9" * 17)
    document = make_document(owner_profile, owner)
    build_spans(document, radiology_lines())

    authenticate(api_client, agent)
    response = api_client.get(CONTENT_PATH.format(uuid=document.uuid))

    assert response.status_code == 403
    assert response.data["error"]["code"] == "patient_role_required"


def test_deleted_document_is_404(api_client):
    user, profile = make_patient(email="o6@example.com", digital_id="1" * 16 + "X")
    document = make_document(profile, user)
    build_spans(document, radiology_lines())
    document.archive_status = MedicalDocument.ArchiveStatus.DELETED
    document.save(update_fields=["archive_status"])

    authenticate(api_client, user)
    response = api_client.get(CONTENT_PATH.format(uuid=document.uuid))

    assert response.status_code == 404


def test_minor_guardian_access_authorized(api_client):
    from tests.test_minor_medical_documents_api import (
        minor as make_minor,
        relationship as make_relationship,
        verified_guardian,
    )

    guardian = verified_guardian(
        email="guardian2@example.com", digital_id="10000000000000013"
    )
    minor_patient = make_minor(digital_id="30000000000000013")
    make_relationship(guardian, minor_patient)
    document = make_document(minor_patient, guardian)
    build_spans(document, radiology_lines())

    authenticate(api_client, guardian)
    response = api_client.get(
        MINOR_CONTENT_PATH.format(minor=minor_patient.uuid, doc=document.uuid)
    )

    assert response.status_code == 200
    data = response.data["data"]
    assert data["content_kind"] == "NARRATIVE"
    assert len(data["sections"]) == 1


def test_minor_unrelated_guardian_is_404(api_client):
    from tests.test_minor_medical_documents_api import (
        minor as make_minor,
        verified_guardian,
    )

    unrelated = verified_guardian(
        email="guardian3@example.com", digital_id="10000000000000014"
    )
    minor_patient = make_minor(digital_id="30000000000000014")
    document = make_document(minor_patient, unrelated)
    build_spans(document, radiology_lines())

    authenticate(api_client, unrelated)
    response = api_client.get(
        MINOR_CONTENT_PATH.format(minor=minor_patient.uuid, doc=document.uuid)
    )

    assert response.status_code == 404
