import hashlib
import io

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from PIL import Image
from pypdf import PdfWriter

from documents.models import StoredFile
from documents.scanning import NotConfiguredFileSecurityScanner
from documents.storage import PrivateMedicalStorage
from documents.validation import inspect_medical_upload


def image_bytes(image_format="PNG", *, size=(2, 2)):
    output = io.BytesIO()
    Image.new("RGB", size, "blue").save(output, format=image_format)
    return output.getvalue()


def pdf_bytes(*, encrypted=False):
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_blank_page(width=72, height=72)
    if encrypted:
        writer.encrypt("secret")
    writer.write(output)
    return output.getvalue()


def upload(name, content, content_type):
    return SimpleUploadedFile(name, content, content_type=content_type)


@pytest.mark.parametrize(
    ("name", "content", "content_type", "expected_mime", "expected_pages"),
    [
        ("report.pdf", pdf_bytes(), "application/pdf", "application/pdf", 2),
        ("scan.jpg", image_bytes("JPEG"), "image/jpeg", "image/jpeg", None),
        ("scan.png", image_bytes("PNG"), "image/png", "image/png", None),
    ],
)
def test_supported_originals_are_validated_without_mutation(
    name,
    content,
    content_type,
    expected_mime,
    expected_pages,
):
    result = inspect_medical_upload(upload(name, content, content_type))

    assert result.content == content
    assert result.mime_type == expected_mime
    assert result.page_count == expected_pages
    assert result.sha256 == hashlib.sha256(content).hexdigest()


@pytest.mark.parametrize(
    ("name", "content", "content_type"),
    [
        ("empty.pdf", b"", "application/pdf"),
        ("broken.pdf", b"%PDF-1.7\nnot-a-pdf\n%%EOF", "application/pdf"),
        ("locked.pdf", pdf_bytes(encrypted=True), "application/pdf"),
        ("image.png", image_bytes("JPEG"), "image/png"),
        ("image.jpg", image_bytes("PNG"), "image/jpeg"),
        ("report.jpg", pdf_bytes(), "image/jpeg"),
        ("payload.exe", image_bytes("PNG"), "image/png"),
        ("trailing.png", image_bytes("PNG") + b"<script>", "image/png"),
        ("trailing.pdf", pdf_bytes() + b"javascript", "application/pdf"),
    ],
)
def test_empty_malformed_spoofed_or_active_trailing_content_is_rejected(
    name,
    content,
    content_type,
):
    with pytest.raises(ValidationError):
        inspect_medical_upload(upload(name, content, content_type))


@override_settings(MEDICAL_FILE_MAX_BYTES=8)
def test_configured_byte_limit_is_enforced_by_bounded_read():
    with pytest.raises(ValidationError, match="size limit"):
        inspect_medical_upload(upload("scan.png", b"x" * 9, "image/png"))


@override_settings(MEDICAL_IMAGE_MAX_PIXELS=4)
def test_configured_image_pixel_limit_is_enforced():
    with pytest.raises(ValidationError, match="dimensions"):
        inspect_medical_upload(
            upload("scan.png", image_bytes("PNG", size=(3, 3)), "image/png")
        )


def test_modern_phone_photo_dimensions_accepted():
    """A real modern phone photo (6400x6400 = 40.96MP, crossing the old 40MP
    ceiling) must pass under the 64MP default. The physical 9.8MB lab JPEG is
    5360x7728 = 41.4MP; the pixel ceiling must not reject legitimate camera
    photos."""
    content = image_bytes("JPEG", size=(6400, 6400))
    result = inspect_medical_upload(
        upload("photo.jpg", content, "image/jpeg")
    )
    assert result.mime_type == "image/jpeg"
    assert result.sha256 == hashlib.sha256(content).hexdigest()


@override_settings(MEDICAL_IMAGE_MAX_PIXELS=64_000_000)
def test_pixel_ceiling_still_enforced_at_new_limit():
    """Raising the ceiling must not disable the check: images above 64MP are
    still rejected (image-bomb protection preserved)."""
    with pytest.raises(ValidationError, match="dimensions"):
        inspect_medical_upload(
            upload(
                "huge.jpg",
                image_bytes("JPEG", size=(8200, 8000)),
                "image/jpeg",
            )
        )


def test_filename_is_path_and_header_safe():
    result = inspect_medical_upload(
        upload("../../evil\r\nname.pdf", pdf_bytes(), "application/pdf")
    )

    assert result.original_filename == "evilname.pdf"
    assert all(character not in result.original_filename for character in "\\/\r\n\0")


def test_private_medical_storage_has_no_public_url(tmp_path):
    with override_settings(MEDICAL_FILE_ROOT=tmp_path):
        storage = PrivateMedicalStorage()
        assert storage.location == str(tmp_path.resolve())
        with pytest.raises(ValueError, match="do not have public URLs"):
            storage.url("medical/report.pdf")


def test_default_scanner_truthfully_reports_not_configured():
    result = NotConfiguredFileSecurityScanner().scan(b"original bytes")

    assert result.status == StoredFile.MalwareScanStatus.NOT_CONFIGURED
    assert result.detail == "Malware scanning is not configured."
