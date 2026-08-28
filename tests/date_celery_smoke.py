"""Run real M9 upload -> extraction/OCR -> date-detection acceptance flows."""

import io
import json
import os
import sys
import time
import uuid
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

import django  # noqa: E402
import fitz  # noqa: E402
from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

django.setup()

from accounts.models import User  # noqa: E402
from documents.models import MedicalDocument  # noqa: E402
from patients.models import PatientProfile  # noqa: E402

FINAL_STATES = {
    MedicalDocument.ProcessingStatus.DATE_DETECTED,
    MedicalDocument.ProcessingStatus.DATE_NOT_FOUND,
    MedicalDocument.ProcessingStatus.AWAITING_CONFIRMATION,
    MedicalDocument.ProcessingStatus.DATE_CONFIRMED,
    MedicalDocument.ProcessingStatus.FAILED,
}


def _native_pdf_upload():
    report = fitz.open()
    page = report.new_page()
    page.insert_text((72, 90), "Synthetic Medical Report", fontsize=18)
    page.insert_text((72, 130), "Report Date: 14/03/2026", fontsize=16)
    page.insert_text(
        (72, 170),
        "Synthetic acceptance content for deterministic integration testing only.",
        fontsize=12,
    )
    page.insert_text(
        (72, 200),
        "No real patient identity or medical information is present in this report.",
        fontsize=12,
    )
    content = report.tobytes()
    report.close()
    return SimpleUploadedFile(
        "synthetic-m9-native.pdf",
        content,
        content_type="application/pdf",
    )


def _ocr_image_upload():
    image = Image.new("RGB", (1500, 420), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 58)
    except OSError:
        font = ImageFont.load_default(size=58)
    draw.text((60, 70), "Synthetic Medical Report", fill="black", font=font)
    draw.text((60, 220), "Report Date: 14/03/2026", fill="black", font=font)
    content = io.BytesIO()
    image.save(content, format="PNG")
    image.close()
    return SimpleUploadedFile(
        "synthetic-m9-ocr.png",
        content.getvalue(),
        content_type="image/png",
    )


def _patient():
    suffix = uuid.uuid4().hex[:12]
    user = User.objects.create_user(
        email=f"m9-celery-{suffix}@example.com",
        password="M9-celery-acceptance-password-2026!",
        status=User.Status.ACTIVE,
    )
    PatientProfile.objects.create(
        user=user,
        digital_id=str(98000000000000000 + int(suffix[:8], 16) % 999999999999999),
        full_name="Synthetic M9 Celery Acceptance",
        date_of_birth=date(1990, 1, 1),
        sex=PatientProfile.Sex.UNSPECIFIED,
        nationality="IQ",
    )
    return user


def _run_flow(client, upload, *, timeout):
    response = client.post(
        "/api/v1/documents/",
        {"file": upload, "document_type": "MEDICAL_REPORT"},
        format="multipart",
        HTTP_HOST="localhost",
    )
    if response.status_code != 201:
        raise RuntimeError(f"Upload failed with status {response.status_code}.")
    document = MedicalDocument.objects.get(uuid=response.data["data"]["uuid"])
    initial_status = document.processing_status
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        document.refresh_from_db()
        if document.processing_status in FINAL_STATES:
            break
        time.sleep(1)
    if document.processing_status not in {
        MedicalDocument.ProcessingStatus.AWAITING_CONFIRMATION,
        MedicalDocument.ProcessingStatus.DATE_CONFIRMED,
    }:
        raise RuntimeError(
            "M9 did not complete; "
            f"document={document.uuid} final_status={document.processing_status} "
            f"failure_code={document.processing_failure_code}."
        )
    suggested = document.date_candidates.get(is_suggested=True)
    if str(suggested.detected_date) != "2026-03-14":
        raise RuntimeError("M9 suggested an unexpected synthetic report date.")
    candidate_response = client.get(
        f"/api/v1/documents/{document.uuid}/date-candidates/",
        HTTP_HOST="localhost",
    )
    if candidate_response.status_code != 200:
        raise RuntimeError("Date-candidate API acceptance request failed.")
    return {
        "document_uuid": str(document.uuid),
        "upload_status_code": response.status_code,
        "initial_status": initial_status,
        "final_status": document.processing_status,
        "extraction_method": document.document_text.extraction_method,
        "suggested_date": str(suggested.detected_date),
        "suggested_type": suggested.candidate_type,
        "source": suggested.source,
        "candidate_count": document.date_candidates.count(),
        "candidate_api_status": candidate_response.status_code,
    }


def main():
    user = _patient()
    client = APIClient()
    client.force_authenticate(user=user)
    print(
        json.dumps(
            {
                "native_pdf": _run_flow(
                    client,
                    _native_pdf_upload(),
                    timeout=120,
                ),
                "ocr_image": _run_flow(
                    client,
                    _ocr_image_upload(),
                    timeout=300,
                ),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
