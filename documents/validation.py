import hashlib
import io
import unicodedata
from dataclasses import dataclass
from pathlib import PurePath

from django.conf import settings
from django.core.exceptions import ValidationError
from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader
from pypdf.errors import PdfReadError

IMAGE_FORMATS = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
}
ALLOWED_EXTENSIONS = {
    "application/pdf": {".pdf"},
    "image/jpeg": {".jpg", ".jpeg"},
    "image/png": {".png"},
}


@dataclass(frozen=True)
class ValidatedMedicalUpload:
    content: bytes
    original_filename: str
    mime_type: str
    extension: str
    sha256: str
    page_count: int | None


def sanitize_filename(name):
    basename = PurePath(str(name).replace("\\", "/")).name
    cleaned = "".join(
        character
        for character in basename
        if unicodedata.category(character)[0] != "C" and character not in "/\\"
    ).strip(" .")
    return (cleaned or "medical-document")[:255]


def _inspect_image(content):
    try:
        with Image.open(io.BytesIO(content)) as image:
            image_format = image.format
            if image.width * image.height > settings.MEDICAL_IMAGE_MAX_PIXELS:
                raise ValidationError("Medical image dimensions exceed the limit.")
            image.verify()
    except ValidationError:
        raise
    except (
        Image.DecompressionBombError,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise ValidationError("Medical image content is malformed.") from exc

    expected = IMAGE_FORMATS.get(image_format)
    if expected is None:
        raise ValidationError("Medical file must be PDF, JPEG, or PNG.")
    exact_end = (
        image_format == "PNG" and content.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82")
    ) or (image_format == "JPEG" and content.endswith(b"\xff\xd9"))
    if not exact_end:
        raise ValidationError("Medical image contains trailing or invalid content.")
    return expected[0], expected[1], None


def _inspect_pdf(content):
    if not content.startswith(b"%PDF-") or not content.rstrip().endswith(b"%%EOF"):
        raise ValidationError("Medical PDF content is malformed.")
    try:
        reader = PdfReader(io.BytesIO(content), strict=True)
        if reader.is_encrypted:
            raise ValidationError("Encrypted medical PDFs are not supported.")
        page_count = len(reader.pages)
        if page_count < 1:
            raise ValidationError("Medical PDF must contain at least one page.")
    except ValidationError:
        raise
    except (PdfReadError, OSError, ValueError, TypeError, IndexError, KeyError) as exc:
        raise ValidationError("Medical PDF content is malformed.") from exc
    return "application/pdf", ".pdf", page_count


def inspect_medical_upload(upload):
    limit = settings.MEDICAL_FILE_MAX_BYTES
    if getattr(upload, "size", 0) == 0:
        raise ValidationError("Medical file cannot be empty.")
    if upload.size > limit:
        raise ValidationError("Medical file exceeds the configured size limit.")

    upload.seek(0)
    content = upload.read(limit + 1)
    upload.seek(0)
    if not content:
        raise ValidationError("Medical file cannot be empty.")
    if len(content) > limit:
        raise ValidationError("Medical file exceeds the configured size limit.")

    declared_mime = getattr(upload, "content_type", "")
    if declared_mime not in ALLOWED_EXTENSIONS:
        raise ValidationError("Medical file must be PDF, JPEG, or PNG.")

    filename = sanitize_filename(upload.name)
    suffix = PurePath(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS[declared_mime]:
        raise ValidationError("Filename extension does not match the declared type.")

    if content.startswith(b"%PDF-"):
        actual_mime, extension, page_count = _inspect_pdf(content)
    else:
        actual_mime, extension, page_count = _inspect_image(content)
    if actual_mime != declared_mime:
        raise ValidationError("Declared MIME type does not match file content.")

    return ValidatedMedicalUpload(
        content=content,
        original_filename=filename,
        mime_type=actual_mime,
        extension=extension,
        sha256=hashlib.sha256(content).hexdigest(),
        page_count=page_count,
    )
