from datetime import date

import pytest

from archive.services import ArchiveQueryService
from documents.date_services import confirm_document_date
from documents.services import update_medical_document
from processing.date_services import process_date_candidates
from tests.archive_helpers import make_document, make_facility, verified_document
from tests.test_date_processing import prepared_document
from tests.test_medical_documents_api import patient_user

pytestmark = pytest.mark.django_db


def test_suggested_but_unconfirmed_date_stays_out_of_chronology_until_confirmed():
    document = prepared_document("Report Date: 14/03/2026\n")
    patient = document.patient
    actor = document.patient.user
    outcome = process_date_candidates(str(document.uuid))
    assert outcome == "AWAITING_CONFIRMATION"

    service = ArchiveQueryService(patient)
    assert service.chronological_queryset({}).count() == 0
    assert service.unconfirmed_count() == 1

    candidate = document.date_candidates.get(is_current=True, is_suggested=True)
    confirmed = confirm_document_date(
        document=document,
        actor=actor,
        candidate_id=candidate.uuid,
    )
    assert confirmed.document_date == date(2026, 3, 14)
    assert confirmed.date_verified is True

    service = ArchiveQueryService(patient)
    assert service.chronological_queryset({}).count() == 1
    assert service.chronological_queryset({}).first().uuid == confirmed.uuid
    assert service.unconfirmed_count() == 0


def test_date_correction_repositions_document_without_copy_or_reprocess():
    document = prepared_document("Report Date: 14/03/2026\n")
    patient = document.patient
    actor = document.patient.user
    process_date_candidates(str(document.uuid))
    candidate = document.date_candidates.get(is_current=True, is_suggested=True)
    confirm_document_date(
        document=document,
        actor=actor,
        candidate_id=candidate.uuid,
    )
    document.refresh_from_db()
    assert document.document_date == date(2026, 3, 14)
    assert patient.medical_documents.count() == 1

    service = ArchiveQueryService(patient)
    assert service.chronological_queryset({"year": 2026, "month": 3}).count() == 1
    assert service.chronological_queryset({"year": 2026, "month": 4}).count() == 0

    file_id = document.stored_file_id
    digest = document.stored_file.sha256
    corrected = confirm_document_date(
        document=document,
        actor=actor,
        manual_date=date(2026, 4, 2),
    )
    assert corrected.document_date == date(2026, 4, 2)
    assert corrected.stored_file_id == file_id
    assert corrected.stored_file.sha256 == digest

    service = ArchiveQueryService(patient)
    assert service.chronological_queryset({"year": 2026, "month": 3}).count() == 0
    assert service.chronological_queryset({"year": 2026, "month": 4}).count() == 1
    assert patient.medical_documents.count() == 1


def test_classification_change_reflects_immediately_in_summary():
    user, patient = patient_user()
    document = verified_document(
        patient,
        user,
        date(2026, 3, 14),
        document_type="OTHER",
    )
    service = ArchiveQueryService(patient)
    assert service.summary()["document_types"] == [
        {"document_type": "OTHER", "count": 1}
    ]
    update_medical_document(
        document=document,
        actor=user,
        metadata={"document_type": "LABORATORY"},
    )
    assert service.summary()["document_types"] == [
        {"document_type": "LABORATORY", "count": 1}
    ]
    assert patient.medical_documents.count() == 1


def test_facility_change_reflects_immediately_in_grouping():
    user, patient = patient_user()
    facility_a = make_facility(name="Facility A")
    facility_b = make_facility(name="Facility B")
    document = verified_document(
        patient,
        user,
        date(2026, 3, 14),
        healthcare_facility=facility_a,
        facility_name="Raw Original",
        location_text="Baghdad / Karkh",
    )
    service = ArchiveQueryService(patient)
    facilities = {row["name"]: row["count"] for row in service.summary()["facilities"]}
    assert facilities == {"Facility A": 1}

    update_medical_document(
        document=document,
        actor=user,
        metadata={"healthcare_facility_id": str(facility_b.uuid)},
    )
    document.refresh_from_db()
    facilities = {row["name"]: row["count"] for row in service.summary()["facilities"]}
    assert facilities == {"Facility B": 1}
    assert document.facility_name == "Raw Original"
    assert document.location_text == "Baghdad / Karkh"
    assert document.healthcare_facility_id == facility_b.uuid
    assert patient.medical_documents.count() == 1


def test_manual_verified_date_enters_chronology_despite_processing_failure():
    user, patient = patient_user()
    document = make_document(patient, user, processing_status="FAILED")
    confirm_document_date(
        document=document,
        actor=user,
        manual_date=date(2026, 4, 2),
    )
    service = ArchiveQueryService(patient)
    assert service.chronological_queryset({"year": 2026, "month": 4}).count() == 1
    assert service.unconfirmed_count() == 0


@pytest.mark.parametrize(
    "status",
    ["AWAITING_CONFIRMATION", "DATE_NOT_FOUND", "FAILED"],
)
def test_unconfirmed_variants_remain_in_bucket_without_manual_date(status):
    user, patient = patient_user()
    make_document(patient, user, processing_status=status)
    service = ArchiveQueryService(patient)
    assert service.chronological_queryset({}).count() == 0
    assert service.unconfirmed_count() == 1
    assert service.summary()["unconfirmed_date_count"] == 1
    assert service.summary()["years"] == []


def test_processing_state_does_not_gate_verified_document_archiveability():
    user, patient = patient_user()
    # A verified manual date with a failed OCR pipeline is still archiveable.
    document = make_document(
        patient,
        user,
        processing_status="FAILED",
        document_date=date(2026, 3, 14),
        date_verified=True,
        date_source="USER_CORRECTED",
    )
    service = ArchiveQueryService(patient)
    assert service.chronological_queryset({}).count() == 1
    assert service.chronological_queryset({}).first().uuid == document.uuid
