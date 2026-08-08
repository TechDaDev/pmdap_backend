import io
from datetime import date

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from pypdf import PdfWriter

from accounts.models import User
from documents.models import MedicalDocument
from patients.models import PatientProfile
from processing.models import DocumentText

pytestmark = pytest.mark.django_db

COLLECTION = "/api/v1/documents/"


def owner():
    user = User.objects.create_user(
        email="text-api-owner@example.com",
        password="A-complex-password-2026!",
        status=User.Status.ACTIVE,
    )
    PatientProfile.objects.create(
        user=user,
        digital_id="12345678901234567",
        full_name="Synthetic API Owner",
        date_of_birth=date(1990, 1, 2),
        sex=PatientProfile.Sex.UNSPECIFIED,
        nationality="IQ",
    )
    return user


def pdf_upload():
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(output)
    return SimpleUploadedFile(
        "detail.pdf", output.getvalue(), content_type="application/pdf"
    )


def persist_text(document, text="PRIVATE EXTRACTED MEDICAL TEXT"):
    return DocumentText.objects.create(
        document=document,
        text=text,
        page_count=1,
        character_count=len(text),
        meaningful_character_count=27,
        usable=True,
        usability_reason="usable_pdf_text",
        has_pages_requiring_ocr=False,
        extraction_method=DocumentText.ExtractionMethod.PDF_TEXT,
        extractor_name="PyMuPDF",
        extractor_version="1.28.0",
        pipeline_version="m7-v1",
    )


def resolve_schema(schema, node):
    if "allOf" in node:
        return resolve_schema(schema, node["allOf"][0])
    if "$ref" not in node:
        return node
    name = node["$ref"].rsplit("/", 1)[-1]
    return schema["components"]["schemas"][name]


def test_detail_exposes_boolean_availability_but_never_text(api_client, tmp_path):
    user = owner()
    api_client.force_authenticate(user=user)
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        created = api_client.post(
            COLLECTION,
            {"file": pdf_upload(), "document_type": "MEDICAL_REPORT"},
            format="multipart",
        )
        document = MedicalDocument.objects.get(uuid=created.data["data"]["uuid"])
        secret = "PRIVATE EXTRACTED MEDICAL TEXT"
        persist_text(document, secret)
        detail = api_client.get(f"{COLLECTION}{document.uuid}/")
        listing = api_client.get(COLLECTION)

    assert detail.status_code == 200
    assert detail.data["data"]["text_available"] is True
    assert secret not in str(detail.data)
    assert "text_available" not in listing.data["data"]["results"][0]


def test_detail_reports_false_when_no_canonical_text(api_client, tmp_path):
    user = owner()
    api_client.force_authenticate(user=user)
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        created = api_client.post(
            COLLECTION,
            {"file": pdf_upload(), "document_type": "MEDICAL_REPORT"},
            format="multipart",
        )
        detail = api_client.get(f"{COLLECTION}{created.data['data']['uuid']}/")

    assert detail.data["data"]["text_available"] is False


def test_openapi_detail_is_additive_and_does_not_document_full_text(api_client):
    schema = api_client.get("/api/v1/schema/", HTTP_ACCEPT="application/json").json()
    detail_operation = schema["paths"]["/api/v1/documents/{document_uuid}/"]["get"]
    response = detail_operation["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    envelope = resolve_schema(schema, response)
    detail = resolve_schema(schema, envelope["properties"]["data"])

    assert detail["properties"]["text_available"]["type"] == "boolean"
    assert {"text", "pages", "extractor_version"}.isdisjoint(detail["properties"])
    assert (
        "text_available"
        not in schema["components"]["schemas"]["MedicalDocument"]["properties"]
    )
