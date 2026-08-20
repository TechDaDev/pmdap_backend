"""Privacy-preserving canonical-content fingerprint for duplicate protection.

Computes an HMAC-SHA256 digest of the normalized canonical OCR body. The digest
is internal only (never exposed to clients). Normalization is deterministic and
conservative: Unicode normalize, lowercase Latin, collapse whitespace. Result
values are preserved verbatim, so a report with different lab values yields a
different fingerprint. Different patients can share identical content (templates)
— duplicate matching is always scoped to one patient.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata

from django.conf import settings

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_canonical(text: str) -> str:
    """Deterministic, conservative normalization of canonical document text."""
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.lower()
    normalized = _WHITESPACE_RE.sub(" ", normalized)
    return normalized.strip()


def _secret() -> bytes:
    value = getattr(settings, "DOCUMENT_FINGERPRINT_SECRET", None)
    if not value:
        value = f"doc-fingerprint::{settings.SECRET_KEY}"
    return value.encode("utf-8")


def content_fingerprint(text: str) -> str:
    """HMAC-SHA256 hex digest of normalized canonical text."""
    normalized = normalize_canonical(text)
    if not normalized:
        return ""
    return hmac.new(_secret(), normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def apply_duplicate_detection(document) -> str | None:
    """Compute + persist the content fingerprint; detect same-patient duplicates.

    Returns the detected duplicate's uuid when the document is a content
    duplicate (status set to DUPLICATE_DETECTED), else None. Never raises for
    the caller; canonical OCR/document state is preserved.
    """
    from documents.models import MedicalDocument, MedicalDocumentEvent
    from documents.services import _record_event

    text = getattr(document, "document_text", None)
    body = text.text if text is not None and text.usable else ""
    fingerprint = content_fingerprint(body)
    document.content_fingerprint = fingerprint

    duplicate = None
    if fingerprint:
        duplicate = (
            MedicalDocument.objects.filter(
                patient=document.patient,
                content_fingerprint=fingerprint,
                archive_status=MedicalDocument.ArchiveStatus.ACTIVE,
            )
            .exclude(pk=document.pk)
            .order_by("created_at")
            .first()
        )

    if duplicate is not None:
        document.processing_status = MedicalDocument.ProcessingStatus.DUPLICATE_DETECTED
        _record_event(
            document,
            MedicalDocumentEvent.EventType.DUPLICATE_DETECTED,
            document.uploaded_by,
            {"existing_document_uuid": str(duplicate.uuid)},
        )
    document.save(
        update_fields=(
            "content_fingerprint",
            "processing_status",
            "updated_at",
        )
    )
    return str(duplicate.uuid) if duplicate is not None else None
