"""Run the real M8 upload -> Celery -> OCR acceptance flow."""

import io
import json
import os
import time
import uuid
from datetime import date

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

import django  # noqa: E402
from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

django.setup()

from accounts.models import User  # noqa: E402
from documents.models import MedicalDocument  # noqa: E402
from patients.models import PatientProfile  # noqa: E402


def _synthetic_upload():
    image = Image.new("RGB", (1400, 360), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 52)
    except OSError:
        font = ImageFont.load_default(size=52)
    draw.text((60, 60), "Patient Report", fill="black", font=font)
    draw.text((60, 180), "Report Date: 14/03/2026", fill="black", font=font)
    content = io.BytesIO()
    image.save(content, format="PNG")
    image.close()
    return SimpleUploadedFile(
        "synthetic-celery-report.png",
        content.getvalue(),
        content_type="image/png",
    )


def main():
    suffix = uuid.uuid4().hex[:12]
    user = User.objects.create_user(
        email=f"m8-celery-{suffix}@example.com",
        password="M8-celery-acceptance-password-2026!",
        status=User.Status.ACTIVE,
    )
    PatientProfile.objects.create(
        user=user,
        digital_id=str(99000000000000000 + int(suffix[:8], 16) % 999999999999999),
        full_name="Synthetic Celery Acceptance",
        date_of_birth=date(1990, 1, 1),
        sex=PatientProfile.Sex.UNSPECIFIED,
        nationality="IQ",
    )
    client = APIClient()
    client.force_authenticate(user=user)
    response = client.post(
        "/api/v1/documents/",
        {"file": _synthetic_upload(), "document_type": "MEDICAL_REPORT"},
        format="multipart",
        HTTP_HOST="localhost",
    )
    if response.status_code != 201:
        raise RuntimeError(f"Upload failed with status {response.status_code}.")
    document = MedicalDocument.objects.get(uuid=response.data["data"]["uuid"])
    initial_status = document.processing_status
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        document.refresh_from_db()
        if (
            hasattr(document, "document_text")
            or document.processing_status == MedicalDocument.ProcessingStatus.FAILED
        ):
            break
        time.sleep(1)
    if (
        document.processing_status == MedicalDocument.ProcessingStatus.FAILED
        or not hasattr(document, "document_text")
    ):
        raise RuntimeError(
            f"OCR did not complete; final status={document.processing_status}."
        )
    detail = client.get(f"/api/v1/documents/{document.uuid}/", HTTP_HOST="localhost")
    extracted = document.document_text
    print(
        json.dumps(
            {
                "upload_status_code": response.status_code,
                "initial_status": initial_status,
                "final_status": document.processing_status,
                "text_available": detail.data["data"]["text_available"],
                "character_count": extracted.character_count,
                "page_count": extracted.page_count,
                "extraction_method": extracted.extraction_method,
                "events": list(
                    document.events.order_by("created_at").values_list(
                        "event_type", flat=True
                    )
                ),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
