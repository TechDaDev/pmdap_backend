import threading
import time
from datetime import date

import pytest
from django.db import close_old_connections, connection

from accounts.models import User
from archive.services import ArchiveQueryService
from documents.date_services import confirm_document_date
from documents.models import MedicalDocument
from documents.services import soft_delete_medical_document, update_medical_document
from tests.archive_helpers import make_facility, verified_document
from tests.test_medical_document_services import actor_and_patient

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.postgresql]


def require_postgresql():
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL-only archive concurrency consistency tests")


def run_reader_writer(reader, writer):
    reader_seen = []
    writer_result = []
    writer_failures = []
    barrier = threading.Barrier(2)

    def run_reader():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            reader_seen.extend(reader())
        finally:
            close_old_connections()

    def run_writer():
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            writer_result.append(writer())
        except Exception as exc:  # exact outcomes asserted per scenario
            writer_failures.append(exc)
        finally:
            close_old_connections()

    reader_thread = threading.Thread(target=run_reader)
    writer_thread = threading.Thread(target=run_writer)
    reader_thread.start()
    writer_thread.start()
    reader_thread.join(timeout=20)
    writer_thread.join(timeout=20)
    assert not reader_thread.is_alive()
    assert not writer_thread.is_alive()
    return reader_seen, writer_result, writer_failures


def reader_chronological_count(patient, filters, iterations=200):
    service = ArchiveQueryService(patient)
    observations = []
    for _ in range(iterations):
        observations.append(service.chronological_queryset(filters).count())
        time.sleep(0.001)
    return observations


def test_archive_read_vs_soft_delete_never_observes_deleted_document(tmp_path):
    require_postgresql()
    actor, patient = actor_and_patient()
    document = verified_document(patient, actor, date(2026, 3, 14))
    document_uuid = document.uuid
    actor_uuid = actor.uuid

    def writer():
        doc = MedicalDocument.objects.get(uuid=document_uuid)
        actor = User.objects.get(uuid=actor_uuid)
        return soft_delete_medical_document(document=doc, actor=actor).uuid

    reader_seen, writer_result, writer_failures = run_reader_writer(
        lambda: reader_chronological_count(patient, {}),
        writer,
    )
    document.refresh_from_db()
    assert not writer_failures
    assert len(writer_result) == 1
    assert set(reader_seen) <= {0, 1}
    assert document.archive_status == MedicalDocument.ArchiveStatus.DELETED
    assert ArchiveQueryService(patient).chronological_queryset({}).count() == 0


def test_archive_read_vs_date_correction_never_double_counts(tmp_path):
    require_postgresql()
    actor, patient = actor_and_patient()
    verified_document(patient, actor, date(2026, 3, 14))
    document_uuid = patient.medical_documents.get().uuid
    actor_uuid = actor.uuid

    def writer():
        doc = MedicalDocument.objects.get(uuid=document_uuid)
        actor = User.objects.get(uuid=actor_uuid)
        return confirm_document_date(
            document=doc,
            actor=actor,
            manual_date=date(2026, 4, 2),
        ).document_date

    def reader():
        service = ArchiveQueryService(patient)
        observations = []
        for _ in range(200):
            march = service.chronological_queryset({"year": 2026, "month": 3}).count()
            april = service.chronological_queryset({"year": 2026, "month": 4}).count()
            assert march + april == 1
            observations.append((march, april))
            time.sleep(0.001)
        return observations

    reader_seen, writer_result, writer_failures = run_reader_writer(reader, writer)
    document = MedicalDocument.objects.get(uuid=document_uuid)
    assert not writer_failures
    assert writer_result == [date(2026, 4, 2)]
    assert document.document_date == date(2026, 4, 2)
    assert patient.medical_documents.count() == 1
    service = ArchiveQueryService(patient)
    assert service.chronological_queryset({"year": 2026, "month": 3}).count() == 0
    assert service.chronological_queryset({"year": 2026, "month": 4}).count() == 1
    assert all(march + april == 1 for march, april in reader_seen)


def test_archive_read_vs_classification_update_stays_consistent(tmp_path):
    require_postgresql()
    actor, patient = actor_and_patient()
    verified_document(patient, actor, date(2026, 3, 14), document_type="OTHER")
    document_uuid = patient.medical_documents.get().uuid
    actor_uuid = actor.uuid

    def writer():
        doc = MedicalDocument.objects.get(uuid=document_uuid)
        actor = User.objects.get(uuid=actor_uuid)
        return update_medical_document(
            document=doc,
            actor=actor,
            metadata={"document_type": "LABORATORY"},
        ).document_type

    def reader():
        service = ArchiveQueryService(patient)
        observations = []
        for _ in range(200):
            summary = service.summary()
            types = {
                row["document_type"]: row["count"] for row in summary["document_types"]
            }
            assert sum(types.values()) == 1
            observations.append(types)
            time.sleep(0.001)
        return observations

    reader_seen, writer_result, writer_failures = run_reader_writer(reader, writer)
    document = MedicalDocument.objects.get(uuid=document_uuid)
    assert not writer_failures
    assert writer_result == ["LABORATORY"]
    assert document.document_type == "LABORATORY"
    assert patient.medical_documents.count() == 1
    types = {
        row["document_type"]: row["count"]
        for row in ArchiveQueryService(patient).summary()["document_types"]
    }
    assert types == {"LABORATORY": 1}
    assert all(sum(row.values()) == 1 for row in reader_seen)


def test_archive_read_vs_facility_reassignment_stays_consistent(tmp_path):
    require_postgresql()
    actor, patient = actor_and_patient()
    facility_a = make_facility(name="Facility A")
    facility_b = make_facility(name="Facility B")
    verified_document(
        patient,
        actor,
        date(2026, 3, 14),
        healthcare_facility=facility_a,
        facility_name="Raw Original",
    )
    document_uuid = patient.medical_documents.get().uuid
    actor_uuid = actor.uuid

    def writer():
        doc = MedicalDocument.objects.get(uuid=document_uuid)
        actor = User.objects.get(uuid=actor_uuid)
        result = update_medical_document(
            document=doc,
            actor=actor,
            metadata={"healthcare_facility_id": str(facility_b.uuid)},
        )
        return result.healthcare_facility_id

    def reader():
        service = ArchiveQueryService(patient)
        observations = []
        for _ in range(200):
            facilities = {
                row["name"]: row["count"] for row in service.summary()["facilities"]
            }
            assert sum(facilities.values()) == 1
            observations.append(facilities)
            time.sleep(0.001)
        return observations

    reader_seen, writer_result, writer_failures = run_reader_writer(reader, writer)
    document = MedicalDocument.objects.get(uuid=document_uuid)
    assert not writer_failures
    assert writer_result == [facility_b.uuid]
    assert document.healthcare_facility_id == facility_b.uuid
    assert document.facility_name == "Raw Original"
    assert patient.medical_documents.count() == 1
    facilities = {
        row["name"]: row["count"]
        for row in ArchiveQueryService(patient).summary()["facilities"]
    }
    assert facilities == {"Facility B": 1}
    assert all(sum(row.values()) == 1 for row in reader_seen)
