import datetime
import uuid
from datetime import date

from django.utils import timezone

from documents.models import MedicalDocument, StoredFile
from facilities.models import AdministrativeRegion, City, Country
from facilities.services import create_healthcare_facility


def make_facility(*, active=True, name="Synthetic Medical Center"):
    country, _ = Country.objects.get_or_create(code="IQ", defaults={"name": "Iraq"})
    region, _ = AdministrativeRegion.objects.get_or_create(
        country=country,
        normalized_name="baghdad",
        defaults={"name": "Baghdad"},
    )
    city, _ = City.objects.get_or_create(
        region=region,
        normalized_name="baghdad",
        defaults={"name": "Baghdad"},
    )
    return create_healthcare_facility(
        name=name,
        country=country,
        region=region,
        city=city,
        facility_type="SPECIALIZED_CENTER",
        active=active,
    )


def make_document(
    patient,
    uploaded_by,
    *,
    document_type="LABORATORY",
    document_date=None,
    date_verified=False,
    date_source="",
    healthcare_facility=None,
    facility_name="",
    location_text="",
    department="",
    physician_name="",
    title="",
    processing_status="UPLOADED",
    archive_status="ACTIVE",
    created_at=None,
):
    digest = uuid.uuid4().hex * 2
    stored = StoredFile.objects.create(
        file=f"medical/{uuid.uuid4().hex}.png",
        original_filename="archive.png",
        mime_type="image/png",
        size_bytes=4,
        sha256=digest,
        page_count=1,
        integrity_status=StoredFile.IntegrityStatus.VALID,
        malware_scan_status=StoredFile.MalwareScanStatus.CLEAN,
    )
    document = MedicalDocument.objects.create(
        patient=patient,
        uploaded_by=uploaded_by,
        stored_file=stored,
        content_sha256=uuid.uuid4().hex,
        document_type=document_type,
        document_date=document_date,
        date_verified=date_verified,
        date_source=date_source,
        healthcare_facility=healthcare_facility,
        facility_name=facility_name,
        location_text=location_text,
        department=department,
        physician_name=physician_name,
        title=title,
        processing_status=processing_status,
        archive_status=archive_status,
    )
    if created_at is not None:
        if isinstance(created_at, datetime.datetime) and timezone.is_naive(created_at):
            created_at = timezone.make_aware(created_at, datetime.UTC)
        elif isinstance(created_at, date) and not isinstance(
            created_at, datetime.datetime
        ):
            created_at = timezone.make_aware(
                datetime.datetime.combine(created_at, datetime.time.min),
                datetime.UTC,
            )
        MedicalDocument.objects.filter(pk=document.pk).update(created_at=created_at)
    return document


def verified_document(patient, uploaded_by, document_date, **kwargs):
    kwargs.setdefault("document_date", document_date)
    kwargs.setdefault("date_verified", True)
    kwargs.setdefault("date_source", "USER_CONFIRMED")
    kwargs.setdefault("processing_status", "DATE_CONFIRMED")
    return make_document(patient, uploaded_by, **kwargs)
