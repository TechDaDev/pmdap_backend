from unittest.mock import patch

import pytest
from django.test import override_settings

from documents.models import MedicalDocument, MedicalDocumentEvent
from tests.test_pdf_extraction_dispatch import (
    actor_and_patient,
    create,
    png_upload,
)

pytestmark = pytest.mark.django_db


def test_image_upload_dispatches_ocr_only_after_commit(
    tmp_path, django_capture_on_commit_callbacks
):
    actor, patient = actor_and_patient()
    with (
        override_settings(MEDICAL_FILE_ROOT=tmp_path),
        patch("processing.tasks.ocr_medical_document.delay") as delay,
        django_capture_on_commit_callbacks(execute=True),
    ):
        document = create(patient, actor, png_upload())
        assert delay.call_count == 0

    assert document.processing_status == MedicalDocument.ProcessingStatus.UPLOADED
    delay.assert_called_once_with(str(document.uuid))
    assert not document.events.filter(
        event_type=MedicalDocumentEvent.EventType.PDF_EXTRACTION_QUEUED
    ).exists()


def test_image_broker_failure_does_not_rollback_document(
    tmp_path, django_capture_on_commit_callbacks
):
    actor, patient = actor_and_patient()
    with (
        override_settings(MEDICAL_FILE_ROOT=tmp_path),
        patch(
            "processing.tasks.ocr_medical_document.delay",
            side_effect=OSError("broker unavailable"),
        ),
        django_capture_on_commit_callbacks(execute=True),
    ):
        document = create(patient, actor, png_upload())

    assert MedicalDocument.objects.filter(pk=document.pk).exists()
    assert document.processing_status == MedicalDocument.ProcessingStatus.UPLOADED
